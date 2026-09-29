"""The review card says which session is on the round, or why it was handed
back (page review step 5)."""

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, events, nudge, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><title>Plan</title><p>Rotate the keys.</p>"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(nudge, "emit", lambda **kw: None)


@pytest.fixture()
def gallery(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def _review(db, agent="seat"):
    page = store.publish(db, agent, "Plan", PAGE)
    return page, decisions.ask_review(db, agent=agent, artifact_id=page.id)


def test_a_live_lease_shows_who_is_listening_and_when_last_seen(db, gallery):
    page, rev = _review(db)
    decisions.claim_round(db, "seat", rev.id, "seat/alpha")
    events.note_read(db, "seat", "seat/alpha")
    html = gallery.get(f"/a/{page.id}").text
    assert "Agent listening · seat/alpha · last seen just now" in html
    with db.connect() as conn:
        conn.execute("update seat_read set last_read_at = now() - interval '5 min'")
    html = gallery.get(f"/a/{page.id}").text
    assert "Agent listening · seat/alpha · last seen 5 min ago" in html


def test_an_expired_lease_shows_nothing(db, gallery):
    page, rev = _review(db)
    decisions.claim_round(db, "seat", rev.id, "seat/alpha")
    with db.connect() as conn:
        conn.execute(
            "update review_round set claim_expires_at = now() - interval '1 min'"
        )
    html = gallery.get(f"/a/{page.id}").text
    assert "Agent listening" not in html and "Handed back" not in html


def test_a_release_shows_who_handed_it_back_and_why(db, gallery):
    page, rev = _review(db)
    decisions.claim_round(db, "seat", rev.id, "seat/alpha")
    decisions.release_round(db, "seat", rev.id, "seat/alpha", "I ran out of context")
    html = gallery.get(f"/a/{page.id}").text
    assert "Handed back by seat/alpha: I ran out of context" in html
    assert "Agent listening" not in html


def test_the_reason_and_session_are_escaped(db, gallery):
    page, rev = _review(db)
    decisions.claim_round(db, "seat", rev.id, "s<b>x")
    decisions.release_round(db, "seat", rev.id, "s<b>x", "<script>alert(1)</script>")
    html = gallery.get(f"/a/{page.id}").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Handed back by s&lt;b&gt;x" in html


def test_a_settled_review_shows_no_presence(db, gallery):
    page, rev = _review(db)
    decisions.claim_round(db, "seat", rev.id, "seat/alpha")
    decisions.answer(db, rev.id, letter="a", by="jeremy")
    html = gallery.get(f"/a/{page.id}").text
    assert "Agent listening" not in html


def test_another_agents_session_never_appears_on_a_page_it_does_not_own(db, gallery):
    mine, _mine_rev = _review(db, "seat")
    other, other_rev = _review(db, "other-agent")
    decisions.claim_round(db, "other-agent", other_rev.id, "other/secret-session")
    events.note_read(db, "other-agent", "other/secret-session")
    html = gallery.get(f"/a/{mine.id}").text
    assert "other/secret-session" not in html and "Agent listening" not in html
    assert "other/secret-session" in gallery.get(f"/a/{other.id}").text
