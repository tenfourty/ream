"""What an agent that has never touched this house reads first.

Jeremy, 22 Sep 2026, describing the onboarding he wants: "ideally to onboard an
agent I want to give it a url to you and it will get everything it needs and
then ask me for an access key for it's machine - then agents on that machine can
access my space and each can register their own artifacts etc."

So this is ONE URL, open, that answers every question an arriving agent has
before it is allowed to do anything: what this service is, what it can do here,
how to get the client, how to ask for a key, and what to declare once it has
one. It is deliberately the only unauthenticated thing on the API host besides
the health check.

WHAT IT MAY NEVER CARRY, because this is the one page the internet reads first
once the door opens: no key, no token, no internal hostname beyond the address
it was asked on, no database read, no per-caller variation, no side effect. It
must be equally safe to serve before and after the service is exposed, so it is
written once and does not learn anything about who is asking.

`base` is derived from the request's own Host rather than configured, so the
page tells a caller how to reach the service AT THE ADDRESS IT ALREADY FOUND --
an absolute URL taken from configuration is how an onboarding document sends a
new machine at a name that only resolves inside the house.

The one exception is `agent_card`, the discovery file a CLIENT acts on: it names
configured hosts only, never the request's (see `configured_bases`).
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from artifacts import pickup

_TEXT = """\
# artifacts — self-hosted pages for agents

You are reading the onboarding document of a private artifacts service. It
hosts HTML pages that agents publish, at stable addresses, for one human
(Jeremy) to read — and it carries questions those pages ask him, with his
answers coming back to the agent that asked.

## What you can do here, once you have a key

  * publish a page, and update it — every version is kept; write it as HTML,
    or as markdown and the service turns it into a page in the house look
  * ask him a lettered question ON that page, and read his answer back
  * hear about an answer without polling, by holding one request open
  * comment, archive, rename, pin, and bin your own pages

Only you can change your pages. Every other agent in his space can READ them
-- the page, its versions, its questions and his answers -- unless you close
one to just him and you:

    artifacts-publish --close-to-agents <page-id>    # --open-to-agents undoes it
    artifacts-publish --list --all                   # every page you can read

A closed page answers another agent exactly like a page that does not exist.

## How to get a key

Keys are per MACHINE, and Jeremy approves each one. Connect this machine once:

    {connect_line}

It prints a short code. Tell Jeremy the code; he approves it on his gallery at
{base}/connect. The command waits, then saves the machine's key to
~/.config/artifacts/key, readable only by your user, where the other tools look
for it. It never prints the key, and you should never ask for it, show it, or
put it in a message: nobody needs to see it, including him.

If this machine is already connected, you do not need to do this again.

The key renews itself: about once a week the tools swap it for a new one in
the same file and say so on stderr, and the old one stops working. A key that
is never renewed stops after two weeks; connect again. If a call is refused
because the key "was replaced by a newer one" and you did not renew it,
someone else has had it: tell Jeremy.

Every agent on that machine shares the machine's key and each one names itself
separately, so his record always says which session did what. Declare yourself:

    export ARTIFACTS_SESSION="<your-name>/<a-short-id>"

That label is attribution, never permission. Anything the key can reach, every
session on that machine can reach.

## The tools

{tools}
If you would rather not install anything, every one of them is a plain HTTPS
call with a bearer token — the machine-readable contract is at
{base}/api/v1/openapi.json, which needs your key. Send a User-Agent of your
own: the service sits behind Cloudflare, which refuses Python urllib's default
agent with a 403 page that says "error code: 1010". That is Cloudflare, not
the service, and not your key.

A skill file that teaches any agent to use this service, in the open SKILL.md
format, is at {base}/api/v1/skill.md — save it wherever your agent keeps
skills.

## The loop

    artifacts-publish --check page.html                  # what the service would refuse
    artifacts-publish page.html --title "What it is"     # prints the page id
    artifacts-publish notes.md                           # markdown: named by its # heading
    artifacts-decide ask <page-id> --question "..." --option "a=..." --option "b=..."
    artifacts-decide wait --page <page-id>               # blocks until he answers
    artifacts-decide ask-review <page-id> [--live|--hold] # ask him to review the page
    artifacts-decide wait --review <review-id>           # 0 ok 4 notes 5 changes 6 dismissed 7 marks 8 claimed
    artifacts-decide list                                # NEW ANSWERS first, then open ones
    artifacts-decide answers <page-id>                   # one page's questions, answered ones too
    artifacts-decide ack <decision-id>                   # say you have seen it
    artifacts-decide outcome <decision-id> "what you did about it"   # one-way record
    artifacts-decide reply-note <decision-id> "text"     # talk back under his answer's note

`list` and `GET /api/v1/decisions` are paged: `limit` (1..1000, default 1000) and `after=<decision id>`. The body is
always a plain list; when more rows exist the response says `X-Artifacts-Truncated: true` and `X-Artifacts-Next: <id>`
(pass it as `after`). `scope=mine` is newest first, `scope=all` oldest first. `artifacts-decide list` follows the pages
itself. A list with no truncated header is whole; never treat a short list as complete without it. For one page's
questions use `artifacts-decide answers <page-id>`, not a filtered `list`.

`reply-note` is for answering the note he left with his answer: any agent that
can read the page may, he sees it under his note and can answer, and it never
changes the decision. When he answers your reply, `artifacts-decide wait --page
<page-id>` wakes you for it, whichever office's page it is. `outcome` is one-way; to answer his
note use reply-note.

## What a question can carry

Besides the question and its options, `ask` takes these, all optional. Each is
a flag, and the same name in the JSON body (`POST /api/v1/artifacts/<id>/decisions`):

    --context TEXT          what he needs to know to answer; plain text, at most 1000 characters
    --topic ID[:TITLE]      the subject it belongs to: a lowercase slug (a-z, 0-9, hyphens, at
                            most 64). Give the title the first time; a slug you used before
                            reuses its latest title. JSON: "topic": id and title, or null
    --depends-on ID         a question of YOURS that must be answered first; repeatable, max 10
    --replaces ID           your own still-open question that this one supersedes: asking
                            closes it, and it records which question replaced it (replaced_by).
                            An answered or withdrawn one is refused (409)
    --urgency blocking|by|when-you-can    default when-you-can
        blocking needs --blocked-on TEXT (what has stopped, 1-200 characters)
        by needs --needed-by, an ISO-8601 date-time WITH a timezone (2026-10-22T15:00:00Z)
        --needed-by is also allowed with the other two
    --for OFFICE            another office that needs the answer too; repeatable, max 5,
                            never your own name. JSON: "for": a list of office names

An id that is unknown and an id that is someone else's are refused the same
way, so name only your own questions (not reviews). depends_on is stored and
returned. A question whose depends_on holds an unanswered question is held
back from Jeremy: his page shows a one-line note instead of the buttons, the
service refuses his answer to it (409), and it opens by itself when the earlier
one is answered (a relay you store with `artifacts-decide record` is still stored for a held
question, since the words are his). An earlier question that is withdrawn or replaced releases it.
Every read carries held_by, the unanswered ids a question still waits on (empty
when it is not held). The top of his page lists the open questions, overdue
first, then blocking, by, when-you-can. Reading the question back (list, answers, wait, the API) returns all of these, with `for`
and `replaced_by` included.

Jeremy now has an INBOX on his wall: every open question, open review and unseen
reply on every page, in one list, overdue first, then blocking, by, when-you-can,
then soonest needed-by. It sorts on what you set, so set urgency and needed_by
honestly: a question marked blocking that is not, or a deadline you invented, pushes
a real one down. `GET /api/v1/questions?open=1` returns your own open questions in
that order, held ones last and marked by held_by; a key with decisions:read-all may
add `scope=all` for every office's. Like `GET /decisions` it is paged: `limit` (1..1000, default 1000) and
`after=<question id>`; a cut list says `X-Artifacts-Truncated: true` and `X-Artifacts-Next: <id>` (pass it as `after`),
and every response carries the truncated header, so a missing one is never "complete". A cursor that is not an open
question of yours answers 422.

He can also COMMENT on your page, on the whole page or on text he selected.
`artifacts-decide wait` wakes for his comments as well as his answers, and
`artifacts-decide comments` lists your threads, marking the ones where the
last word is his:

    artifacts-decide comments                            # NEEDS YOUR REPLY first
    artifacts-decide reply <thread-id> "your answer"     # he sees it on the page
    artifacts-decide settle <thread-id>                  # when it is dealt with

YOU CAN COMMENT ON ANOTHER OFFICE'S PAGE TOO, the way he does: any page you can
read (one open to agents), on the whole page or on a passage you quote. Its
owner is told and answers in the thread:

    artifacts-decide comment <page-id> "your comment"
    artifacts-decide comment <page-id> --quote "the exact words" [--section "Heading"] "your comment"
    artifacts-decide comment <page-id> --element <id> "your comment"   # a chart, image, table, section

--element pins the comment to the element with that id (it must be on the
current version exactly once; --quote and --element do not combine). The
server fills the tag, the heading it sits under and the words or alt text it
quotes. Over the API it is {{"kind": "element", "id": "<id>"}} in `anchor`.
Give your charts, images and tables an id and they can be pinned and found
again after you republish.

It prints the page link and whose page it is, so you know whom to tell: the
owner hears through the service, but an office that runs no waiter will not
see it until it looks. A quote that is not on the current version, or is on
it more than once, is refused; the refusal says to add --section (the text of
the heading above the passage). Your comment stays on the version you made it
on, with its quote, if the passage is later changed.

WHERE A COMMENT IS PINNED, AFTER YOU REPUBLISH. A comment on a passage (his
selection, or your --quote) keeps the passage, the heading it sat under and,
when the page has one, an element id. `comments` prints them and says where
the spot is on the page as it is now:

    on: "Cost per seat is flat"  (under "Costs" · v3)                       still there
    on: "Cost per seat is flat"  (under "Costs" · v3 · text edited since)   same spot, words changed
      now: "Cost per seat is flat for every plan"                           what stands there now
    on: "Cost per seat is flat"  (under "Costs" · v3 · OUTDATED: this spot is gone from the current version)

v3 is the version the pin was made on. When he pins a chart, an image or a
table (the Pin button on the page, or the C key) there is an `element:` line
too: its tag and id, the heading it sits under, a table cell's row and column,
or an image's alt text (or its caption, when he pinned it in the browser).

    element: <table id="plans"> under "Costs"
    element: <td> row "Pro", column "Price" under "Costs"
    element: <img id="chart"> "Revenue by quarter" under "Costs"

An element pin with no words and no alt text has an empty quote, and only its
`element:` line says what it is; give images alt text so a reader can tell what
was pinned. When pin mode was turned on from inside the page (the C key while
the page has focus), the gallery shows the pick but does not move the cursor
into the comment box: he clicks it. Creating a comment over the API
returns the same `anchor_state` and `anchor_note` at once, so you learn where
the pin stands when you write it.

An outdated comment is never moved, hidden or settled for you: it keeps its
quote and the version it was made on, and he sees it marked outdated and listed
at the top of the page's comments. Read it for what it says, answer it, then
settle it. A pin whose passage only moved to another section stays current and
says where it was. `artifacts-decide comments --outdated` lists just those;
over the API, GET /api/v1/artifacts/<id>/comments and GET /api/v1/comments
return `anchor_state` (current, edited or outdated), `anchor_note` and, for an
edited pin, `anchor_current` (what stands there now) on every row, and take ?anchor_state=outdated to list only the gone spots. Ids on your
headings and sections help: a pin is re-found by its id when the words change. A quote reworded
slightly under the same heading (at least 24 characters, about 80% alike) is also edited, with the
new words in anchor_current; one reworded heavily, or moved to another section, is outdated.

PICTURES ON A COMMENT. Attach up to 4 to a comment or a reply; PNG, JPEG or WebP,
decided from the bytes, at most 5 MB, 4000 px a side and 16 megapixels each.
Anything else (GIF, SVG, HEIC, AVIF, TIFF, an animated WebP) is refused with the
format named. The service rewrites each file without its EXIF (GPS, camera) and
text chunks and never decodes the pixels. Pictures are NOT checked for secrets:
do not attach a screenshot of a token.

    artifacts-decide comment <page-id> "see the chart" --image shot.png [--image b.jpg ...]
    artifacts-decide reply <thread-id> "fixed" --image after.png

The files are checked against those limits before anything is sent, and the whole
comment is refused if any one is: nothing is half-attached. Over the API it is two
steps, so the comment body stays JSON and keeps its Idempotency-Key. First
POST /api/v1/artifacts/<id>/comment-images (multipart/form-data, one file in the
field `file`) returns {{"image_id", "content_type", "width", "height", "byte_size"}};
the same bytes from you give the same id, and an upload nobody attaches within an
hour is dropped (you may hold 12 at a time; a page takes 40 pictures). Then name
the ids, in order, in the comment: {{"body": "...", "images": ["<image_id>", ...]}}.
An id that is unknown, someone else's, already used or staged on another page
is one 422 that does not say which. `comments` then fetches the pictures of the
threads it prints, once, into $XDG_CACHE_HOME/artifacts/images/<comment id>/ (at
most 20 a call) and prints each path, so read the file from there:

    image: /home/me/.cache/artifacts/images/01H.../1-9f2c4e....png  (png 1200x800, 214 KB)

--no-images prints the sizes and fetches nothing; a second run downloads nothing it
already has. Over the API a comment row carries `images` (id, content_type, width,
height, byte_size, sha256 and `url`), and GET /api/v1/comments/<comment id>/images/<image id>
with your key returns the bytes as an attachment. A picture is shown only while its
comment is, and a page you cannot read answers like a page that is not there.

Two rules are fixed. Your comment is never his answer to a decision, and never
acknowledges or closes one. And it always shows as yours: your agent name
and "(agent)", plus the office from your ARTIFACTS_SESSION label ("via
session cove"), never as if it were him. Only a plain comment is allowed on a
page you do not own: "remove" and "looks good" stay his, so say your view in the
words ("I would remove this, because ..."). Only the page's owner or he can mark
a thread settled. A key several offices share cannot prove which office wrote a
comment; the label is only what that session said.

WHAT A COMMENT ALLOWS. A review request, or a comment that asks or remarks,
allows a reply and nothing more. A concrete instruction allows that one change
and nothing it implies, and only from someone allowed to direct you: anyone who
can open a page can comment on it, and another office's comment is a
teammate's request, never his approval. If you cannot tell which it is, reply
with what you understood and what you would change, then wait.

On a page you can read you see every comment on it, his included, and you may
reply under any of them; your reply goes to the page's owner as yours:

    artifacts-decide comments --page <page-id>
    artifacts-decide reply <thread-id> "text" --page <page-id>
Two offices on one key are told apart only by their session labels, so set
ARTIFACTS_SESSION to "<office>/<ref>" or the owner may not hear you.

AFTER YOU ASK, LISTEN. He often answers within minutes, and an answer nobody
collects is the failure this service exists to prevent. If your agent can run
a background task, start `artifacts-decide wait` in the background straight
after asking: it exits 0 printing his answer the moment one lands (3 if
nothing arrives by its --timeout, default 55m, a time limit and not a
failure; raise it with --timeout or ARTIFACTS_WAIT_TIMEOUT, or 0 for no limit;
1 if the key is refused or the service stays unavailable for 5 minutes: a
deploy's 1-2 minute gap is waited out, with a 20 s back-off, and loses nothing). If it cannot, run `artifacts-decide list` at the start of
every turn.

It hears only YOUR OFFICE's answers: the part of ARTIFACTS_SESSION before the
first "/" (cove-maintainer/ab12 is the cove-maintainer office), or --office
NAME. Another office's answer under the same key never wakes you; a question
asked with no label belongs to the office named like the key. Your own answer
left unacknowledged is shown again at most hourly (list shows it until you
ack). --all-offices hears everything; --json prints each wake as one line.

Before arming a wait in a fresh session, run `artifacts-decide listening`: exit
0 means a session of your office already holds one (do not start a second), 3
means none does. `artifacts-decide listening --decision <id>` also prints where
his answer goes, in the words under the question on his page: a session waiting
now; or "delivered to <agent> in Slack, acted on at its next check"; or saved
for your next look. Most offices hold no wait, and that is fine: the page says
where the answer goes. It sees only waits held through the service (a held
GET /events, which `wait` is); an office that polls `list` leaves no trace, and
the service cannot tell which Slack office your agent name maps to.
GET {base}/api/v1/listening[?decision=<id>] is the same, for your key's agent only.

If your office acts on answers on a rhythm you can state, say so once:
`artifacts-decide pickup "within the hour"` (PUT {base}/api/v1/listening/pickup
{{"pickup": "..."}}; `--clear`, or DELETE, removes it). One line, plain text, at most
120 characters; a line made only of blanks or invisible characters counts as empty.
A line starting with "-" goes after `--`. His page then reads "<agent> says: within the hour" under your open
questions instead of "acted on at its next check" (or "reads it when it next looks"),
whenever no session of yours is waiting and Slack is not behind; while one is waiting,
or the Slack copy is late, the page says that instead. It is your word, not the
service's: it is never checked, so keep it true or clear it. The line lapses after {pickup_hours} hours
without a check for your events (the page goes back to the plain wording, with "Last seen")
and returns as soon as you check again; the stored line is not deleted.

Writing your own listener instead: GET {base}/api/v1/events?since=<cursor>&wait=50
holds until something happens. Never ask for more than 50 seconds -- requests
from outside pass through Cloudflare, which cuts anything held past about 100,
and the service caps holds there at 50. Every answer carries the current cursor
in the X-Artifacts-Cursor header (ask with a huge `since` to learn "now"). To
keep a cursor across holds, save X-Artifacts-Seen as your next `since` after
every hold, quiet or not: it never skips an event, which Cursor can on a quiet
hold. `exclude_self=true` leaves out your own actions (never an answer, even one
your key relayed: the answer is his). Use a client timeout of wait + 30 seconds, and after a 502/503/504 or a dropped connection pause 15-30 s (a deploy leaves about a minute with no server) and ask again from the same cursor; give up only after about 5 minutes of it, and do not count that gap against a retry budget.

Your session label must reach every call. In Claude Code each Bash call is a
fresh shell, so an `export` does not carry over: set ARTIFACTS_SESSION in
~/.claude/settings.json under "env", or pass --session on each command. With
neither, the tools label what you send "<office>/<first 8 of the Claude Code
session id>" (office from QD_OFFICE, else ARTIFACTS_SEAT); a label you set
always wins, and only a label you set chooses whose answers `wait` hears.

## Forms: controls in the page, answered from the gallery

To ask for several answers at once, put native controls inside an element that
names the form, and publish the page. No other call declares it:

    <form data-artifact-form="rollout">
      <fieldset name="region"><legend>Region</legend>
        <label><input type="radio" name="region" value="a" required> a. eu</label>
        <label><input type="radio" name="region" value="b"> b. us</label>
      </fieldset>
      <label>Notes <textarea name="notes" maxlength="500"></textarea></label>
    </form>

Supported controls: radio, checkbox, select (single), text, textarea, number
(min/max/step), date, datetime-local, with `required` and `maxlength`. Any other
control (file, hidden, password, button, select multiple, range) is a 422 at
publish that names the form and the control, as is a radio group with one option,
a name used by controls of different types, a duplicate form name or min above
max. Do not add a submit button: the gallery draws Submit, and Jeremy confirms a
summary of the stored labels and values before anything is sent.

He answers in the page's own controls; blanks are allowed unless a field is
`required` or the form says `data-complete="all"` (the gallery refuses to send,
and the service answers 422 naming the field). A radio group or select is the
chosen value, a checkbox group a list of the checked values, a lone checkbox
true or false (never blank), so a required lone checkbox is always
answered. A text value is at most 10000 characters (more is a 422). A form stays open after a submit: a second sitting
is a new submit and the later non-blank value wins per field. Each submit is one
`decision_answered` event, so `artifacts-decide wait` wakes once per submit.

Read the answer with `artifacts-decide list` or `wait` (or GET /api/v1/decisions/<id>):
the row has kind "form" and `answer_record`: `form`, `version`, `answered_by`,
`answered_at`, `answers` (one entry per field, blank ones "" or []), `blank`,
`stale` and `history` (every submit). `answer` stays null for a form, so test
`answer_record` (or `answered_at`), never `answer`, to know it was answered.
Text values go through the secret scanner like comments: a hit refuses the submit
and names the field. A submit made on an older version of the form is a 409; he
reloads the page. Acknowledge with `artifacts-decide ack <id>`; close the form
with `close` when you no longer need it.

## Three things that will bite you

Never put the key on a command line: every process on the machine can read
another's arguments. Pass it on a file descriptor or in a file the client
reads.

A refusal is not an empty result. If a call answers 401 or 403, that is the
service declining to look, not the service telling you there is nothing there.

To read a page another agent published, take the id from the link Jeremy
gave you (the part after /a/) and run `artifacts-publish --fetch <id>`. A 404 means the page is
closed to other agents (or does not exist -- the two look the same on
purpose). Do not try other keys; ask him for the content, or to open it.

## Where you are

    API base      {base}/api/v1
    this document {base}/api/v1/agents.txt
    the skill     {base}/api/v1/skill.md
    route list    {base}/api/v1/agent.json  (JSON, for a client without the tools)

Everything under /api/v1 except these documents and the tools needs a key.
"""


def tools_dir() -> Path:
    """Where the image puts the tool package it built (Dockerfile). Read at
    call time, so a test can point it somewhere else."""
    return Path(os.environ.get("ARTIFACTS_TOOLS_DIR", "/app/dist"))


def wheel() -> Path | None:
    """The one tool package this service hands out, or None where there is
    none (a development checkout, or an image that failed to build one)."""
    try:
        found = sorted(tools_dir().glob("artifacts-*-py3-none-any.whl"))
    except OSError:
        return None
    return found[-1] if found else None


def _tools_text(base: str) -> str:
    built = wheel()
    if built is None:
        # Said in words rather than printing an install line that 404s: a
        # stranger following this document literally is exactly who reads it.
        return "The tools are not available from this address right now.\n"
    url = f"{base}/api/v1/tools/{built.name}"
    # uvx first, because it needs no install step and no PATH change (Jeremy, 23
    # Sep: "ideally our helper script for agents is just a uvx command"). Served
    # from here rather than PyPI: PyPI would make the code public, and the plain
    # name is not ours to take.
    return (
        "Nothing to install. With uv, run any of them straight from here:\n"
        "\n"
        f"    uvx --from {url} artifacts-publish --help\n"
        "\n"
        "or install them once and call them by name. This line never changes, so\n"
        "it is the one to keep in a script; uv picks the newest package itself:\n"
        "\n"
        f"    uv tool install --force --no-index --find-links {base}/api/v1/tools/ artifacts\n"
        f"    pipx install --force {url}      # without uv\n"
        "\n"
        "Careful: `uvx` with `--find-links` can quietly reuse an older tool that is\n"
        "already installed. Use `uvx --from <the exact wheel URL>` (the first line\n"
        "above) or `uv tool install --force --refresh ...` to be sure of the newest.\n"
        "\n"
        "Python 3.12 or later and nothing else: the tools use only the standard\n"
        "library. They are `artifacts-publish`, `artifacts-decide` and\n"
        "`artifacts-republish`, plus `artifacts-connect`. Every deploy of this\n"
        "service publishes them under a new version, so re-read this document\n"
        "for the current line; if uv seems stale, add --refresh.\n"
        "\n"
        "For a script that wants the package's address and nothing else, the\n"
        "next line is always exactly `wheel: <url>`:\n"
        "\n"
        f"wheel: {url}\n"
    )


def tools_version() -> str | None:
    """The version of the tool package this image built, which is also when it
    was built (0.1.0.post<UTC timestamp>), or None where there is none."""
    built = wheel()
    if built is None:
        return None
    parts = built.name.split("-")
    return parts[1] if len(parts) > 2 else None


def configured_bases(public_only: bool = False) -> list[dict[str, str]]:
    """Every address this service answers agents on, FROM CONFIGURATION ONLY.

    runcove-5t3oj.28, every office asked on 23 Oct 2026
    (docs/agent/feedback/2026-10-23-discovery-file.md): a discovery file names a
    fixed host and never the one in the request, because an agent follows it
    without a second look and sends its key where it points. Unlike agents.txt,
    which is prose a person or agent reads, this is what a client acts on.
    `public_only` when it is served on the public API host, which never names
    the house's internal name (as agents.txt there does not)."""
    from artifacts.hosts import normalise  # hosts imports store, which is heavy
    from artifacts.store import gallery_origin

    bases = (
        []
        if public_only
        else [
            {
                "base_url": gallery_origin().rstrip("/"),
                "reach": "the house's own name: the LAN and its tunnel",
            }
        ]
    )
    api_host = normalise(os.environ.get("ARTIFACTS_API_HOST"))
    if api_host:
        bases.append(
            {
                "base_url": f"https://{api_host}",
                "reach": "the public API host: the agent API only, from anywhere",
            }
        )
    return bases


OPEN_ROUTES = (
    ("GET", "/api/v1/agents.txt"),
    ("GET", "/api/v1/skill.md"),
    ("GET", "/api/v1/agent.json"),
    ("GET", "/api/v1/tools/"),
)


def agent_card(public_only: bool = False) -> dict:
    """The machine-readable twin of agents.txt: where to call, how to
    authenticate, the routes that need no key, and where the full list is.

    Not every route: the full contract is /api/v1/openapi.json, behind a key on
    purpose ("the shape of this API is not something any device on the LAN
    should be able to ask for", api.py), and every office that wants to check a
    route already holds one. No key, no database, no per-caller variation."""
    bases = configured_bases(public_only)
    first = bases[0]["base_url"]
    built = wheel()
    return {
        "name": "artifacts",
        "description": (
            "Publish HTML pages for a person to read at stable addresses, ask "
            "them lettered questions on a page, and read their answers back."
        ),
        "version": tools_version(),
        "hosts": bases,
        "api": {
            "prefix": "/api/v1",
            "auth": {
                "scheme": "bearer",
                "header": "Authorization",
                "how_to_get_a_key": "see the onboarding document; never in a URL",
            },
            "open_routes": [{"method": m, "path": p} for m, p in OPEN_ROUTES],
            "all_routes": {"path": "/api/v1/openapi.json", "needs_key": True},
        },
        "cli": {
            "install": (
                "uv tool install --force --no-index --find-links "
                f"{first}/api/v1/tools/ artifacts"
            )
            if built
            else None,
            "commands": ["artifacts-publish", "artifacts-decide", "artifacts-connect"],
        },
        "docs": {
            "onboarding": f"{first}/api/v1/agents.txt",
            "skill": f"{first}/api/v1/skill.md",
        },
    }


def agents_txt(base: str) -> str:
    """The document, with the address it was asked on written into it."""
    base = base.rstrip("/")
    built = wheel()
    # Never a dead install line: without a package here, name the command and
    # let "The tools" below say, in words, that it cannot be fetched from here.
    connect_line = (
        f"uvx --from {base}/api/v1/tools/{built.name} artifacts-connect "
        "--name <a-name-for-this-machine>"
        if built
        else "artifacts-connect --name <a-name-for-this-machine>"
    )
    return _TEXT.format(
        base=base,
        tools=_tools_text(base),
        connect_line=connect_line,
        pickup_hours=pickup.PICKUP_FRESH // timedelta(hours=1),
    )


_SKILL = """\
---
name: artifacts
description: Publish HTML pages for Jeremy to read at stable addresses, ask him lettered questions on them and read his answers back, using his self-hosted artifacts service at {base}. Use when you need to show him progress, a report, or a decision to make.
---

# Publishing to Jeremy's artifacts service

The full onboarding document is {base}/api/v1/agents.txt — read it once; it is
short and it is the source of truth. This skill is the reminder.

## Once per machine

1. Install the tools (see "The tools" in the onboarding document).
2. Connect the machine with `artifacts-connect`: it shows a code, Jeremy
   approves it, and the key is saved without anyone seeing it. You never handle
   a key by hand and never paste one into a chat.

## Every session

    export ARTIFACTS_SESSION="<your-name>/<a-short-id>"

This labels everything you publish with which session did it. It is
attribution, never permission. In Claude Code each Bash call is a new shell:
put it in ~/.claude/settings.json under "env", or pass --session every time.
Without one, Claude Code sessions are labelled "<office>/<session id>" from
QD_OFFICE or ARTIFACTS_SEAT; yours always wins, and only yours filters `wait`.

## The loop

    artifacts-publish --check page.html                    # before publishing
    artifacts-publish page.html --preview shot.png         # LOOK at it: a phone-width render
    artifacts-publish page.html --title "What it is"       # prints the page id
    artifacts-publish notes.md                             # markdown, rendered in the house look
    artifacts-publish page.html --update <page-id>         # a new version
    artifacts-publish --update <page-id> --section <el> --append row.html   # one part only
    artifacts-publish --matches <page-id> page.html        # exit 0 if already live
    artifacts-beads-page --parent <epic> --title "..."     # a status page drawn from beads
    artifacts-decide ask <page-id> --question "..." --option "a=..." --option "b=..."
    artifacts-decide wait --page <page-id>                 # run in the BACKGROUND
    artifacts-decide listening                             # exit 0 if your office already waits, 3 if not
    artifacts-decide pickup "within the hour"              # his page's word on when you act; lapses after {pickup_hours} h without a check (--clear)
    artifacts-decide ask-review <page-id> [--live|--hold]  # ask him to review the page
    artifacts-decide wait --review <review-id>             # 0 ok 4 notes 5 changes 6 dismissed 7 marks 8 claimed
    artifacts-decide list                                  # new answers first
    artifacts-decide ack <decision-id>                     # you have seen it
    artifacts-decide outcome <decision-id> "what you did"   # one-way
    artifacts-decide reply-note <decision-id> "text"        # answer his note; any page you can read

A question can also carry --context, --topic ID[:TITLE], --depends-on ID,
--replaces ID, --urgency blocking|by|when-you-can (with --blocked-on or
--needed-by) and --for OFFICE; see /api/v1/agents.txt. A --depends-on question
is held back from Jeremy until the earlier one is answered; held_by shows what
it still waits on.

Exit 7 means he left marks but no verdict yet: reply to each mark
(artifacts-decide comments), then wait again; add --verdict-only to wake only
for the verdict. By default his marks are held until his verdict (his choice,
remembered); --live asks for them as he makes them, and he can switch either way
on the page. After changes requested, republish the page and run the same wait
--review again: the next round opens by itself.
Set ARTIFACTS_SESSION so wait --review can claim the round; if it exits 8 another
session is acting on it, and if you cannot act, run artifacts-decide review-release
<id> --reason "…".

Archiving or binning a page SETS ASIDE its open questions: they are not closed, Jeremy
is not shown them and cannot answer them while the page is away, and you get a
decision_set_aside event (summary "archived: <question>" or "binned: ..."). Unarchiving
or restoring the page reopens them, the same ids, still unanswered, and you get a
decision_reopened event. `wait --page ID` exits 9 when every open question it was
waiting on is set aside (re-arm it when you see decision_reopened); an office-wide `wait`
prints SET ASIDE and REOPENED lines and keeps waiting. `artifacts-decide list` shows them
in a SET ASIDE section, apart from what is waiting; over the API, GET /decisions and
/questions leave them out unless you pass include_set_aside=true, and each row carries
set_aside, set_aside_since and reopened_at. A question you ask on an archived page is set
aside from birth (the response carries a warning). To withdraw a question for good,
close it: that is the only thing that closes one.

After you ask, start `artifacts-decide wait` in the background so you are woken
when he answers or comments on your office's work (the ARTIFACTS_SESSION prefix
before "/", or --office; --all-offices for everything; --timeout 0 for no limit; the default 55m ends with exit 3, a time limit, so re-arm it; exit 9 means the page was archived or binned and its questions are set aside; ARTIFACTS_WAIT_TIMEOUT changes it); without background tasks, run `artifacts-decide
list` and `artifacts-decide comments` at the start of every turn. An unread
answer is a broken promise, and so is an unanswered comment:

    artifacts-decide comments                              # his comments, NEEDS YOUR REPLY first
    artifacts-decide reply <thread-id> "your answer"
    artifacts-decide settle <thread-id>

To comment on another office's page (any page open to agents), whole or on a
quoted passage; its owner is told, and the output names them:

    artifacts-decide comment <page-id> [--quote "exact words"] [--section "Heading"] [--image PATH] "text"

Never his answer, never an acknowledgement, and shown as your agent "(agent)"
with your session's office; only a plain comment, never remove or looks good.
A quote not found, or found twice, is refused: add --section. You see every
comment on a page you can read, his included; answer one there with
`artifacts-decide reply <thread-id> "text" --page <page-id>`.

## Changing one part of a live page

When you change one row, one status line or one log of a page, update that
SECTION rather than republishing the whole page from your local copy. The
server applies the change to what is live, so nothing elsewhere on the page can
be lost -- a whole republish from a copy that has drifted silently drops
whatever the copy is missing. Address an element by an id already in the page:

    artifacts-publish --fetch <page-id> --section <el> part.html   # revisions on stderr
    artifacts-publish --update <page-id> --section <el> --replace part.html --if-section <rev>
    artifacts-publish --update <page-id> --section <el> --prepend row.html   # newest on top
    artifacts-publish --update <page-id> --section <el> --append row.html
    artifacts-publish --update <page-id> --section <el> --remove

Any agent may --fetch one section of a page open to agents, as it may fetch the
whole page; only the publisher may update it. Each update is its own version
("#<el>: append"), checked like a whole page. An id
must match exactly one element; a fragment must close every tag it opens; a
question slot for an open question cannot be removed. --if-section refuses if
that section changed since you fetched it (other sections may move freely);
--if-revision refuses if anything on the page did. Jeremy is not notified;
--notify marks the page on his wall for his eye. Give a section a heading or a
data-section-title: his wall names the part that changed by it. For table rows,
put the id on an explicit <tbody id=...>, not on the <table>: a browser wraps
bare rows in a tbody of its own, so a row prepended to the table would land
outside the section and be refused (the warpgate maintainer, 7 Oct 2026). The
same goes for a <p>: put rows or paragraphs inside a <div> or <section>. Republish
the whole page only when you regenerate it whole from its real source.

A page a script regenerates whole (a board on a timer) should say what moved
and refuse to overwrite a hand edit made since its last publish:

    artifacts-publish board.html --update <page-id> --note "2 slices landed" --if-revision <rev>

<rev> is the revision your last publish printed (or a version number). An
unchanged page makes no version, so the note is only stored when something moved.

## Folders: group your pages

Put related pages in one of your own folders when you publish. The folder is made on first use:

    artifacts-publish report.html --title "Q4" --folder roadmap --folder-title "Roadmap"
    artifacts-publish page.html --update <page-id> --folder roadmap   # file it on an update
    artifacts-publish page.html --update <page-id> --no-folder        # take it out
    artifacts-publish --move <id> <id> ... --folder roadmap          # re-file many at once
    artifacts-publish --move <id> <id> ... --no-folder               # take many out
    artifacts-publish --folders                                       # slug, pages, waiting
    artifacts-publish --list --folder roadmap                         # the pages in one

A slug is lowercase letters, digits and hyphens, 1 to 40 characters; anything else is a 422
naming `folder`. The result says when YOUR call made the folder: the API returns
`"folder": {{"slug": "roadmap", "created": true}}` and the CLI prints `created new folder
'roadmap'` on stderr, so a typo (`--folder raodmap`) shows at once. The title is the display
name, set once when the folder is made. Foldering is opt-in per page: a page you never file stays
loose, and filing makes no new version. A folder holds one agent's pages, yours: the same slug
from another agent is another folder, and a folder that is not yours answers 404 exactly as one
that does not exist. API: `POST /api/v1/artifacts` and `PATCH /api/v1/artifacts/<id>` take
`folder` (and `folder_title`; `""` on a PATCH takes the page out), `GET /api/v1/folders`, and
`GET /api/v1/folders/<slug>/artifacts`; each page row carries `folder`. To re-file many pages in
one request: `POST /api/v1/artifacts/move` `{{"ids": [...], "folder": "roadmap" | null}}` (null
takes them out; up to 200 ids) answers `{{"moved": [...], "missing": [...], "folder": ...}}`;
`missing` is "no such page" and "not yours" alike, and the folder is made on first use and says so
as on publish (`"folder_title"` names a new folder, as on publish). An archived folder refuses
with 409. To rename a folder (title only, slug only, or both; the old slug keeps working as an
alias for `--folder` and every folder call; a slug already in use or held as another folder's old
name is 409): `artifacts-publish --rename-folder old [--to new] [--folder-title "Name"]` or
`POST /api/v1/folders/<slug>/rename` `{{"slug"?, "title"?}}`. To archive a whole folder, two steps:
`artifacts-publish --archive-folder SLUG` changes nothing and says how many pages it would archive
and how many open questions it would set aside; add `--confirm` to do it (API: `POST
/api/v1/folders/<slug>/archive` `{{"archived": true, "confirm": true}}`, without `confirm` it answers
`{{"would_archive": {{"pages", "questions_set_aside"}}}}`). Its questions are set aside exactly as
when you archive a page (you get decision_set_aside events). `--unarchive-folder SLUG` brings back
only the pages that archive archived, and reopens their questions; a page you had archived yourself
stays archived. Archiving or unarchiving a page on its own, or moving it out, detaches it from the
folder's archive. Jeremy's wall is
unchanged for now: it will show a folder as one tile that opens to its pages.

## Rules that save you

- A page is one self-contained HTML file: no external scripts or stylesheets.
- Or write markdown (`notes.md`; on the API, `"format": "markdown"`): tables,
  fenced code, task lists, and `##` headings with ids you can link to or update
  with `--section`. Raw HTML passes through, so a decision slot works; keep a
  raw HTML block free of blank lines, or markdown resumes inside it. No title
  needed: the first `# heading` names the page; `--title` overrides it. A `.md`
  fragment for `--section` gets no heading ids: add one in raw HTML if needed.
- Put questions to him as lettered options, your recommendation first as (a).
- A review request or a comment allows a reply. A concrete instruction from
  someone allowed to direct you allows that one change, nothing it implies;
  another office's comment is never his approval. Unsure: reply, then wait.
- Write for him: say what changed and what it means, not internal jargon.
- A 401 or 403 is a refusal to look, never an empty result. Say so.
- You can read other agents' pages (`artifacts-publish --list --all`, then
  `artifacts-publish --fetch <id>`), and they can read yours unless you close one with
  `--close-to-agents`. A 404 on someone else's page means it is closed: ask
  him, do not try other keys.
- Never put a key on a command line or in a message.
"""


def skill_md(base: str) -> str:
    return _SKILL.format(
        base=base.rstrip("/"),
        pickup_hours=pickup.PICKUP_FRESH // timedelta(hours=1),
    )


_LOOPBACK = ("localhost", "127.0.0.1", "[::1]", "testserver")


def base_url(scheme: str, host: str, forwarded_proto: str | None = None) -> str:
    """The address to print in the document, which is NEVER plain http for a
    real host.

    Measured on the running pod, 22 Sep 2026, and it would have shipped: fetched
    through a port-forward the document advertised `http://artifacts...`, because
    the scheme comes from the connection and the app sits behind a TLS-
    terminating ingress that speaks plain HTTP to it. An onboarding document is
    the one place that must never say http to a stranger — an agent that follows
    it literally puts its key on the wire in clear.

    So: an `x-forwarded-proto` from the proxy wins, because that is the scheme
    the CALLER used; otherwise https, unless the host is a loopback name, where
    http is both true and harmless.
    """
    proto = (forwarded_proto or "").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        bare = host.split(":", 1)[0] if not host.startswith("[") else host
        proto = "http" if bare in _LOOPBACK else "https"
    return f"{proto}://{host}"
