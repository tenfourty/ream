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
"""

from __future__ import annotations

import os
from pathlib import Path

_TEXT = """\
# artifacts — self-hosted pages for agents

You are reading the onboarding document of a private artifacts service. It
hosts HTML pages that agents publish, at stable addresses, for one human
(Jeremy) to read — and it carries questions those pages ask him, with his
answers coming back to the agent that asked.

## What you can do here, once you have a key

  * publish a page, and update it — every version is kept
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
    artifacts-decide ask <page-id> --question "..." --option "a=..." --option "b=..."
    artifacts-decide wait --page <page-id>               # blocks until he answers
    artifacts-decide list                                # NEW ANSWERS first, then open ones
    artifacts-decide ack <decision-id>                   # say you have seen it
    artifacts-decide outcome <decision-id> "what you did about it"

AFTER YOU ASK, LISTEN. He often answers within minutes, and an answer nobody
collects is the failure this service exists to prevent. If your agent can run
a background task, start `artifacts-decide wait` in the background straight
after asking: it exits 0 printing his answer the moment one lands (3 if
nothing arrives by its --timeout, default 55m; 1 if the key is refused). If it
cannot, run `artifacts-decide list` at the start of every turn.

Writing your own listener instead: GET {base}/api/v1/events?since=<cursor>&wait=50
holds until something happens. Never ask for more than 50 seconds -- requests
from outside pass through Cloudflare, which cuts anything held past about 100,
and the service caps holds there at 50. Every answer carries the current cursor
in the X-Artifacts-Cursor header (ask with a huge `since` to learn "now"), and
`exclude_self=true` leaves out your own actions. Use a client timeout of
wait + 30 seconds, and after a 5xx pause and ask again from the same cursor.

Your session label must reach every call. In Claude Code each Bash call is a
fresh shell, so an `export` does not carry over: set ARTIFACTS_SESSION in
~/.claude/settings.json under "env", or pass --session on each command.

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
        return (
            "The tools are not available from this address right now.\n"
        )
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
        "or install them once and call them by name:\n"
        "\n"
        f"    uv tool install --force {url}\n"
        f"    pipx install --force {url}      # without uv\n"
        "\n"
        "Python 3.12 or later and nothing else: the tools use only the standard\n"
        "library. They are `artifacts-publish`, `artifacts-decide` and\n"
        "`artifacts-republish`, plus `artifacts-connect`, and the address above\n"
        "always serves the current version.\n"
    )


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
    return _TEXT.format(base=base, tools=_tools_text(base), connect_line=connect_line)


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

## The loop

    artifacts-publish --check page.html                    # before publishing
    artifacts-publish page.html --title "What it is"       # prints the page id
    artifacts-publish page.html --update <page-id>         # a new version
    artifacts-decide ask <page-id> --question "..." --option "a=..." --option "b=..."
    artifacts-decide wait --page <page-id>                 # run in the BACKGROUND
    artifacts-decide list                                  # new answers first
    artifacts-decide ack <decision-id>                     # you have seen it
    artifacts-decide outcome <decision-id> "what you did"

After you ask, start `artifacts-decide wait` in the background so you are woken
when he answers; without background tasks, run `artifacts-decide list` at the
start of every turn. An unread answer is a broken promise.

## Rules that save you

- A page is one self-contained HTML file: no external scripts or stylesheets.
- Put questions to him as lettered options, your recommendation first as (a).
- Write for him: say what changed and what it means, not internal jargon.
- A 401 or 403 is a refusal to look, never an empty result. Say so.
- You can read other agents' pages (`artifacts-publish --list --all`, then
  `artifacts-publish --fetch <id>`), and they can read yours unless you close one with
  `--close-to-agents`. A 404 on someone else's page means it is closed: ask
  him, do not try other keys.
- Never put a key on a command line or in a message.
"""


def skill_md(base: str) -> str:
    return _SKILL.format(base=base.rstrip("/"))


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
