"""The client protocol level: the one number that says which generation of tools this is.

Not the package version: that has been 0.1.0 since the first commit and does not change with a
release, and the module's install time says when a copy was made, not what it can do. So the
tools carry a level, bumped BY HAND in the commit that needs it.

* `CLIENT_LEVEL` is what this copy of the tools is. Sent on every call as `X-Ream-Client`
  (and `X-Artifacts-Client`).
* `MIN_CLIENT_LEVEL` is the oldest level the server wants. A call from an older tool (or from
  one that sends no level, which is level 0) is answered normally and flagged out of date.

Raise `MIN_CLIENT_LEVEL` only when a change needs new tools (a verb, a header or a route an old
copy gets wrong). Raise `CLIENT_LEVEL` to the same number in that commit. Standard library
only: the command-line tools import this.
"""

CLIENT_LEVEL = 3
MIN_CLIENT_LEVEL = 2

# STEP 2 SWITCH (comments-setting rollout, 9 Nov 2026). While False a NEW page published without
# a comments setting is accepted (stored null, read as on); `ream publish` does not insist on
# --comments. Setting True turns on the refusal everywhere at once: the server (422 on
# /api/v1/pages, both words, and the MCP publish_page tool) and the CLI. Flip it in the commit
# that also raises CLIENT_LEVEL/MIN_CLIENT_LEVEL if tools need to be told again. Tests marked
# `needs_comments_required` run only when it is True. Standard library only, like this module.
COMMENTS_REQUIRED = True

# Request header the tools send, and the response headers the server answers with, each
# under both prefixes (the old one stays for good).
CLIENT_HEADER = "X-Ream-Client"
OUTDATED_HEADER = "X-Ream-Client-Outdated"
MIN_HEADER = "X-Ream-Client-Min"
SOURCE_HEADER = "X-Ream-Install-Source"


def old_name(header: str) -> str:
    """The `X-Artifacts-` spelling of a `X-Ream-` header."""
    return "X-Artifacts-" + header.removeprefix("X-Ream-")


def level_of(value: str | None) -> int:
    """The level a request header states; anything missing or not a whole number is 0."""
    try:
        return max(0, int((value or "").strip()))
    except ValueError:
        return 0
