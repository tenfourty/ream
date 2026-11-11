"""Receipts slice 2: the gallery's strip of an agent's receipt, and lapsed computed by one
rule for the API and the page (receipts design 4.3). `now` is a parameter; nothing sleeps."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import decisions, listening, receipts, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
SESSION = "ops-seat/lane-3"


def test_lapsed_is_false_up_to_and_at_the_instant_and_true_after():
    end = T0 + timedelta(hours=4)
    assert not receipts.is_lapsed("working", end, end - timedelta(seconds=1))
    assert not receipts.is_lapsed("working", end, end)
    assert receipts.is_lapsed("working", end, end + timedelta(seconds=1))


def test_only_working_with_an_expiry_can_lapse_and_strings_and_naive_times_read():
    past = T0 - timedelta(days=1)
    for state in ("seen", "done", "blocked", "wont_do"):
        assert not receipts.is_lapsed(state, past, T0)
    assert not receipts.is_lapsed("working", None, T0)
    assert receipts.is_lapsed("working", past.isoformat(), T0)
    assert receipts.is_lapsed("working", past.replace(tzinfo=None), T0)
    assert not receipts.is_lapsed("working", "not a time", T0)


@pytest.fixture()
def settled(db):
    art = store.publish(db, "ops-seat", "Board", PAGE)
    row = decisions.ask(
        db,
        agent="ops-seat",
        artifact_id=art.id,
        question="Which?",
        options=[{"letter": "a", "label": "First"}, {"letter": "b", "label": "Second"}],
    )
    decisions.answer(db, row.id, letter="a", by="jeremy")
    return art, row


def _declare(db, row, **fields):
    with db.connect() as conn, conn.transaction():
        return receipts.declare(conn, "ops-seat", row.id, session=SESSION, **fields)


def _age_working(db, row, claimed_ago: timedelta, lasted: timedelta):
    """Move the stored claim into the past: said `claimed_ago`, good for `lasted`."""
    said = datetime.now(UTC) - claimed_ago
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "update receipt set created_at = %s, expires_at = %s where subject_id = %s",
            (said, said + lasted, row.id),
        )


def _page(db, art):
    return TestClient(create_gallery_app(db)).get(f"/a/{art.id}").text


def test_the_api_receipt_reports_lapsed_by_the_same_rule(db, settled):
    _art, row = settled
    _declare(db, row, state="working", note="bead x", lifetime="30m")
    with db.connect() as conn:
        live = receipts.current_map(conn, [row])[row.id]
        end = datetime.fromisoformat(live["expires_at"])
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        before = receipts.current_map(conn, [row], end - timedelta(seconds=1))[row.id]
        after = receipts.current_map(conn, [row], end + timedelta(seconds=1))[row.id]
        hist = receipts.history(conn, row.id, end + timedelta(minutes=1))
    assert live["lapsed"] is False and before["lapsed"] is False
    assert after["lapsed"] is True and after["state"] == "working"
    assert hist[-1]["lapsed"] is True


def test_a_working_strip_names_agent_session_and_note(db, settled):
    art, row = settled
    _declare(db, row, state="working", note="bead x")
    page = _page(db, art)
    assert f"Working, ops-seat, {SESSION}. bead x" in page
    assert "not picked up yet" not in page
    assert "Self-declared by the agent" in page
    assert "working lapsed" not in page


def test_a_lapsed_strip_says_how_long_ago_with_the_note(db, settled):
    art, row = settled
    _declare(db, row, state="working", note="bead x", lifetime="30m")
    _age_working(db, row, timedelta(hours=5, minutes=10), timedelta(minutes=30))
    page = _page(db, art)
    assert "Lapsed: ops-seat said working 5 hours ago. bead x" in page
    assert '<span class="unread">working lapsed</span>' in page


def test_a_renewal_clears_the_lapse(db, settled):
    art, row = settled
    _declare(db, row, state="working", note="first", lifetime="30m")
    _age_working(db, row, timedelta(hours=5), timedelta(minutes=30))
    _declare(db, row, state="working", note="again")
    page = _page(db, art)
    assert "Lapsed" not in page and "Working, ops-seat" in page and "again" in page


def test_done_blocked_and_wont_do_strips(db, settled):
    art, row = settled
    _declare(db, row, state="done", note="shipped", link="https://x.test/pr/1")
    page = _page(db, art)
    assert f"Answered, ops-seat, {SESSION}. shipped" in page
    assert '<a href="https://x.test/pr/1"' in page
    _declare(db, row, state="blocked", note="needs a key")
    assert f"Blocked, ops-seat, {SESSION}. needs a key" in _page(db, art)
    _declare(db, row, state="wont_do", note="out of scope")
    assert f"ops-seat, {SESSION}. out of scope" in _page(db, art)


def test_no_receipt_and_seen_look_as_before(db, settled):
    art, row = settled
    assert '<span class="unread">not picked up yet</span>' in _page(db, art)
    decisions.acknowledge(db, "ops-seat", row.id, session=SESSION)
    page = _page(db, art)
    assert "ops-seat picked this up" in page and "not picked up yet" not in page


def test_receipt_line_wording_is_pure_and_takes_now():
    r = {
        "state": "working",
        "agent": "cove",
        "session": "cove/lane-3",
        "note": "",
        "at": (T0 - timedelta(hours=1)).isoformat(),
        "lapsed": True,
    }
    assert listening.receipt_line(r, "", T0) == "Lapsed: cove said working 1 hour ago."
    r["lapsed"] = False
    assert listening.receipt_line(r, "", T0) == "Working, cove, cove/lane-3."
