"""Viewer-typed text on its way into somewhere that is not a browser.

A page's own content is HTML and a browser renders it; that is not this. This
is free text a human typed — a decision note, a comment — travelling to an
agent's context, a terminal, a log line or a chat message, where there is no
renderer and a control character is not inert. A newline can fake a second
line in a mailbox format. An escape sequence can move the cursor, recolour, or
overwrite what was printed above it. A bell rings.

So the rule is: this text is DATA, never structure, and it is flattened to
data before it leaves the service — not by whoever happens to be printing it.
The nudge path had this right and had it alone; a consumer reading the event
feed got the raw string, which meant every future client would have had to
re-implement this or quietly not (homelab-ij8.55).

Sanitising at the boundary, not at the point of display, is the whole point.
There is no list of consumers to keep in step.
"""

import re

# Every C0 control character except the whitespace ones `sanitize` already
# turns into a plain space by splitting on whitespace (tab, LF, CR, FF, VT),
# plus the C1 range — an escape sequence, a bell, a NUL: anything that could
# reorder or forge structure in a terminal, a mailbox line or an agent's
# context.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# A summary is a line in someone else's list, not a document.
SUMMARY_LIMIT = 200


def sanitize(text: str, limit: int = SUMMARY_LIMIT) -> str:
    """Collapse every run of whitespace to one space, strip any remaining
    C0/C1 control character, and cap at `limit` characters with a trailing
    ellipsis.

    Deliberately not escaping: this text is going somewhere with no renderer,
    so an escaped `\\x1b` would just be noise a human has to read past. The
    character is removed because there is nothing it could legitimately mean
    here.
    """
    collapsed = " ".join(text.split())
    cleaned = _CONTROL_CHARS.sub("", collapsed)
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned
