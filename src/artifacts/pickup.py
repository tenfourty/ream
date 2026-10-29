"""The pickup-line window, kept free of server imports: the CLI and the
onboarding text read it too, and the CLI is installed without the `server` extra."""

from __future__ import annotations

from datetime import timedelta

# How long a pickup line is shown after the agent last asked for its events:
# the reader's choice of window (may become 2 hours); the only place it is set.
PICKUP_FRESH = timedelta(hours=24)
