"""The nudge seam: an interface, and nothing behind it yet.

A future slice wires `emit` to an HTTP POST into the operator's inbox on baba -- a
separate service, in a separate repo, reachable over the LAN and gated by a credential
this repo never holds. Until that slice lands, `emit` does the one thing worth doing on
its own: log at DEBUG, so every call site in this codebase (the answer path today, more
later) is already written against the shape the wired version will have, and switching
the body over is a change contained entirely to this file.

The one rule that matters more than the shape: a nudge must never turn a successful
request into a failed one. Answering a decision is what Jeremy came to do; telling some
other system about it afterwards is a courtesy, not a precondition. So `emit` never
raises, never blocks (nothing here does I/O yet, but the promise holds once it does --
the eventual HTTP call must be fire-and-forget, not awaited inline in the request path),
and never changes what the caller returns. A caller that wraps this in its own
try/except in case emit misbehaves has misread this module: the guarantee is made HERE,
once, so nobody else has to.
"""

import logging
import os

_log = logging.getLogger("artifacts.nudge")


def emit(kind: str, *, artifact_id: str, title: str, agent: str, summary: str) -> None:
    """Record that something worth telling the operator about happened. `summary`
    is caller-prepared prose (the answer path passes the chosen letter and label,
    capped at 200 characters by the caller -- this function does not re-cap it, so a
    future caller with a different shape does not silently inherit a limit that was
    never its own).

    `ARTIFACTS_NUDGE_URL` is read, not because this function does anything with it
    yet, but so the deployment can set the placeholder now and the wiring slice needs
    no new config key when it arrives. Reading it here is deliberately inert: no
    request is made, no value is logged (an operator inbox URL is not a secret, but
    logging it from every single nudge call would be noise for no benefit).
    """
    try:
        os.environ.get("ARTIFACTS_NUDGE_URL")
        _log.debug(
            "nudge: kind=%s artifact=%s title=%s agent=%s summary=%s",
            kind,
            artifact_id,
            title,
            agent,
            summary,
        )
    except Exception as exc:  # noqa: BLE001 -- deliberately swallowed, not re-raised: see
        # module docstring, "never raises" is the one guarantee this function makes.
        # Only the exception's TYPE is logged (never its message or a value it might
        # carry), matching store.py's own rule for a scanner failure.
        _log.debug("nudge: emit() itself failed: %s", type(exc).__name__)
