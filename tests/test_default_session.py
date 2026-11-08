"""A session that declares no label gets one from the agent tool's environment
(runcove-5t3oj.25), but only for what it sends: which office a waiter listens
for stays the declared label alone."""

from types import SimpleNamespace

from ream.client import declared_session, default_session, session_label
from ream.decide import _office

SID = "48147c82-1111-2222-3333-444455556666"


def test_the_default_is_the_quarterdeck_office_and_the_short_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setenv("QD_OFFICE", "gg-ops")
    monkeypatch.setenv("ARTIFACTS_SEAT", "something_else")
    assert default_session() == "gg-ops/48147c82"


def test_without_quarterdeck_the_seat_names_the_office(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setenv("ARTIFACTS_SEAT", "warpgate_maintainer")
    assert default_session() == "warpgate-maintainer/48147c82"


def test_no_session_id_or_no_office_means_no_default(monkeypatch):
    monkeypatch.setenv("QD_OFFICE", "cove")
    assert default_session() is None
    monkeypatch.delenv("QD_OFFICE")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    assert default_session() is None


def test_an_explicit_label_always_wins(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setenv("QD_OFFICE", "quarterdeck")
    assert session_label("qd-maintainer/abc") == "qd-maintainer/abc"
    monkeypatch.setenv("REAM_SESSION", "qd-maintainer/env")
    assert session_label() == "qd-maintainer/env"


def test_the_default_never_narrows_what_an_unlabelled_waiter_hears(monkeypatch):
    # quarterdeck's office names differ from labels already in use
    # ("artifacts" vs "artifacts-maintainer"): a default here would hide answers.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setenv("QD_OFFICE", "artifacts")
    args = SimpleNamespace(all_offices=False, office=None, session=None)
    assert declared_session() is None
    assert session_label() == "artifacts/48147c82"
    assert _office(args) is None
