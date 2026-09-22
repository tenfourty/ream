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

Your pages are yours: a page belonging to another agent answers exactly the
same as a page that does not exist, so a key cannot be used to discover what
else is here.

## How to get a key

Keys are issued per MACHINE, by Jeremy, by hand. There is no self-service, and
no endpoint here will give you one.

Ask him, in your own conversation with him, for an artifacts key for this
machine. He runs one command and gives you a token that starts `art_`. Put it
in a file only your user can read, and point the client at it.

Every agent on that machine shares the machine's key and each one names itself
separately, so his record always says which session did what. Declare yourself:

    export ARTIFACTS_SESSION="<your-name>/<a-short-id>"

That label is attribution, never permission. Anything the key can reach, every
session on that machine can reach.

## The client

    uv tool install artifacts --from git+{forge}/jeremy/artifacts

It gives you `artifacts-publish`, `artifacts-decide` and `artifacts-republish`.
If you cannot install it, every one of them is a plain HTTPS call with a bearer
token — the machine-readable contract is at {base}/api/v1/openapi.json, which
needs your key.

## The three calls that matter

    artifacts-publish page.html --title "What it is"     # returns the page id
    artifacts-decide ask <page-id> --question "..." --option "a=..." --option "b=..."
    artifacts-decide list                                 # his answers, as they arrive

To be TOLD rather than to poll, hold a request open — it returns the moment
something happens, or empty after the wait you asked for:

    GET {base}/api/v1/events?since=<cursor>&wait=60

Keep the `seq` of the last row you saw as your cursor. The feed never skips and
never repeats, so a cursor is all you need to catch up after being away.

## Two things that will bite you

Never put the key on a command line: every process on the machine can read
another's arguments. Pass it on a file descriptor or in a file the client
reads.

A refusal is not an empty result. If a call answers 401 or 403, that is the
service declining to look, not the service telling you there is nothing there.

## Where you are

    API base      {base}/api/v1
    this document {base}/agents.txt
    health        {base}/healthz

Nothing else is served on this address.
"""


def agents_txt(base: str, forge: str = "https://git.tenfourty.site") -> str:
    """The document, with the address it was asked on written into it."""
    return _TEXT.format(base=base.rstrip("/"), forge=forge.rstrip("/"))
