"""Runs the exploratory walkthrough in tests/walk_gallery.py.

Skipped unless REAM_WALK is set: it is a bug-finding harness rather than a
test of a behaviour, and the behaviours it has found are asserted in their own
files. Keeping it in the repo rather than in a scratch directory because the
next person to change the gallery should be able to re-run it in one command.
"""

import os

import pytest
from walk_gallery import walk


@pytest.mark.skipif(
    not os.environ.get("REAM_WALK"),
    reason="set REAM_WALK=1 to walk the gallery",
)
@pytest.mark.usefixtures("fresh_schema")
def test_walk(db):
    findings = walk(db.dsn)
    assert findings == [], "\n".join(findings)
