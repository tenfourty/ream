"""The service says when a page was last published, so no agent has to type it.

WHY (homelab-ij8.70). Jeremy, 20 Sep 2026, looking at a status board: "date/
timestamp seems wrong and not in the local TZ (can we make that easier for
agents so when they edit they can insert a last updated timestamp easier if
they want it?)". It read 08:53 UTC while his own clock said 10:53.

The timezone was the visible half. The half worth fixing is that the page was
carrying a time some agent had written down -- and an agent writing a time from
memory, or from the shape of the conversation rather than from a clock, has been
wrong repeatedly. The service is the one participant that cannot be wrong about
when a version was published: it holds `version.created_at`, written by the
database at the moment of the write.

SUBSTITUTED WHEN THE PAGE IS SERVED, NOT WHEN IT IS STORED. Storing the
formatted time would fold the clock into the body, and the body is hashed to
produce `revision` -- so two identical publishes would differ, a new revision
would be minted on every no-op republish, and the work to make an identical
republish a no-op would be defeated by the stamp alone. Serving it instead keeps
the stored bytes exactly as the agent wrote them, and lets an old version show
ITS OWN publication time rather than the newest one's.

AN HTML COMMENT, because it fails safe. A marker that renders as text would show
a reader a raw token if anything went wrong; a comment that is never substituted
is simply invisible. A missing stamp is a small loss. A wrong one is the thing
this exists to prevent.
"""

from __future__ import annotations

import html
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from artifacts import settings

#: What an agent writes in its page. Deliberately verbose and namespaced: a page
#: is arbitrary HTML from an arbitrary author, and a short marker like `{{date}}`
#: would collide with a templating language somebody else is already using.
MARKER = "<!--artifacts:updated-->"

#: "20 September 2026, 11:04 CEST". The zone is named because a bare local time
#: is unambiguous only to someone standing in it, and these pages are read from
#: a phone in one place and a terminal in another.
FORMAT = "%-d %B %Y, %H:%M %Z"


def display_timezone() -> tzinfo | None:
    """The zone pages are rendered in, read at call time.

    Read at call time rather than at import, for the same reason
    `content_domain()` is: a module-level read of the environment freezes at the first
    import, so no test can override it and the deployment has to set it before
    anything imports this module.

    Unset means the host's own zone, which is right for a homelab whose reader
    and whose server are in the same place, and which cannot go stale the way a
    hard-coded city would when the host moves. An unknown name is NOT an error
    worth failing a page render for -- it falls back to the host, because a page
    that will not load teaches nobody anything about a misconfigured variable.
    """
    name = settings.current().display_tz
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def format_for_display(when: datetime) -> str:
    """A naive datetime is assumed to be UTC, which is what the database hands
    back when a column is read without its zone -- guessing local there would
    shift every stamp by the host's offset and look plausible while doing it."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo("UTC"))
    return when.astimezone(display_timezone()).strftime(FORMAT)


def apply(body: str, when: datetime | None) -> str:
    """Replace every marker in `body` with `when`, rendered for a reader.

    `when` of None leaves the markers alone: unknown is not the same as absent,
    and an invisible comment is a better answer than a blank where a date should
    be. Escaped on the way in even though the text is generated entirely here --
    a zone name comes from the environment, and the rule that nothing reaches a
    page unescaped is worth more than the one case where it happens not to
    matter.
    """
    if when is None or MARKER not in body:
        return body
    return body.replace(MARKER, html.escape(format_for_display(when)))
