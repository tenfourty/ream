"""An answer Jeremy gave off the page, recorded as what it is.

homelab-ij8.78. He answers in a terminal or on Telegram as often as on a page.
Before this the only way to get those words into the service was to press the
button for him, which mints an answer ON THE PAGE indistinguishable from one he
tapped — the kind of record that gets believed later by an agent that was not
there. Two seats hit exactly that on 20 Sep 2026 and both refused, correctly,
leaving four real answers stranded with their decisions still open.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body>x</body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def asked(db):
    keys.mint(db, agent="seat")
    art = store.publish(db, "seat", "Board", PAGE)
    return decisions.ask(
        db,
        agent="seat",
        artifact_id=art.id,
        question="Which?",
        options=[{"letter": "a", "label": "Alpha"}, {"letter": "b", "label": "Beta"}],
        recommended="a",
    )


def test_a_relayed_answer_is_recorded_with_its_provenance(db, asked):
    row = decisions.record_elsewhere(
        db,
        asked.id,
        letter="b",
        note="start with (b) but then (a) if we can",
        source="telegram",
        recorded_by="homelab-ops",
    )
    assert row.answer == "b"
    assert row.answer_note == "start with (b) but then (a) if we can"
    assert row.answer_source == "telegram"
    assert row.recorded_by == "homelab-ops"
    assert row.answered_by == "jeremy", "the words are his; the recording is not"


def test_a_relay_can_never_claim_to_be_a_tap(db, asked):
    """The single most important assertion here. 'page' means he tapped it
    himself, and a relay able to claim it would be the exact forgery this call
    exists to prevent."""
    with pytest.raises(decisions.InvalidSource):
        decisions.record_elsewhere(
            db, asked.id, letter="a", note="x", source="page", recorded_by="seat"
        )


def test_an_invented_source_is_refused(db, asked):
    with pytest.raises(decisions.InvalidSource):
        decisions.record_elsewhere(
            db, asked.id, letter="a", note="x", source="he nodded", recorded_by="seat"
        )


def test_an_anonymous_relay_is_refused(db, asked):
    """An unattributed relay is the same forgery wearing a different hat."""
    with pytest.raises(decisions.InvalidSource):
        decisions.record_elsewhere(
            db, asked.id, letter="a", note="x", source="terminal", recorded_by=""
        )


def test_a_tapped_answer_says_it_was_tapped(db, asked):
    """So no reader ever has to interpret a missing value. Before this, `answer
    is not null and answer_source is null` would have been a state someone had
    to guess about — and the safe guess and the true one are not the same."""
    row = decisions.answer(db, asked.id, letter="a", note="fine", by="jeremy")
    assert row.answer_source == "page"
    assert row.recorded_by is None


def test_the_two_are_never_the_same_shape(db, asked):
    """A reader that knows nothing about relays must not be able to mistake one
    for a tap, and vice versa."""
    relayed = decisions.record_elsewhere(
        db, asked.id, letter="b", note="his words", source="terminal", recorded_by="ops"
    )
    tapped = decisions.answer(db, asked.id, letter="a", note="", by="jeremy")
    assert relayed.answer_source != tapped.answer_source
    assert relayed.recorded_by and not tapped.recorded_by


def test_a_letter_that_is_not_an_option_is_refused(db, asked):
    with pytest.raises(decisions.InvalidAnswer):
        decisions.record_elsewhere(
            db, asked.id, letter="z", note="x", source="terminal", recorded_by="ops"
        )


def test_a_closed_decision_is_refused(db, asked):
    decisions.close(db, "seat", asked.id)
    with pytest.raises(store.NotFound):
        decisions.record_elsewhere(
            db, asked.id, letter="a", note="x", source="terminal", recorded_by="ops"
        )


def test_the_owning_agent_still_hears_about_it(db, asked):
    """To the agent waiting, the fact is the one it has been waiting for: Jeremy
    decided. The relay must not be silent just because it came another way."""
    from artifacts import events

    decisions.record_elsewhere(
        db, asked.id, letter="b", note="his words", source="telegram", recorded_by="ops"
    )
    rows = events.list_for_agent(db, agent="seat", since=0)
    assert rows, "the owning agent got no event for a relayed answer"
    assert any("relayed from telegram" in r.summary for r in rows), (
        "the feed must name the relay, or a reader is misled about what happened"
    )


# --- through the API --------------------------------------------------------


def test_the_route_takes_the_recording_seat_from_the_key(client, db, asked):
    """Never from the body. The accountable party is not something a caller
    gets to nominate."""
    token, _ = keys.mint(db, agent="homelab-ops")
    got = client.post(
        f"/api/v1/decisions/{asked.id}/record",
        json={
            "letter": "b",
            "note": "his words",
            "source": "telegram",
            "recorded_by": "someone-else",
        },
        headers=auth(token),
    )
    assert got.status_code == 200
    assert got.json()["recorded_by"] == "homelab-ops"


def test_the_route_refuses_page_as_a_source(client, db, asked):
    token, _ = keys.mint(db, agent="ops")
    got = client.post(
        f"/api/v1/decisions/{asked.id}/record",
        json={"letter": "a", "note": "x", "source": "page"},
        headers=auth(token),
    )
    assert got.status_code == 422
    assert "page" in got.json()["detail"]


def test_the_route_records_when_he_said_it(client, db, asked):
    token, _ = keys.mint(db, agent="ops")
    got = client.post(
        f"/api/v1/decisions/{asked.id}/record",
        json={
            "letter": "b",
            "note": "his words",
            "source": "telegram",
            "said_at": "2026-09-20T11:38:00+00:00",
        },
        headers=auth(token),
    ).json()
    # Compared as INSTANTS, not as strings: the service renders timestamps in
    # its own zone, so "2026-09-20T13:38:00+02:00" is the same moment that went
    # in and a string comparison would fail on a correct value.
    from datetime import datetime

    assert datetime.fromisoformat(got["said_at"]) == datetime.fromisoformat(
        "2026-09-20T11:38:00+00:00"
    )
    assert datetime.fromisoformat(got["answered_at"]) != datetime.fromisoformat(
        got["said_at"]
    ), "when he said it and when it was written down are different facts"


def test_a_malformed_said_at_is_refused_rather_than_ignored(client, db, asked):
    token, _ = keys.mint(db, agent="ops")
    got = client.post(
        f"/api/v1/decisions/{asked.id}/record",
        json={"letter": "a", "note": "x", "source": "terminal", "said_at": "yesterday"},
        headers=auth(token),
    )
    assert got.status_code == 422


def test_the_route_needs_a_key(client, asked):
    got = client.post(
        f"/api/v1/decisions/{asked.id}/record",
        json={"letter": "a", "note": "x", "source": "terminal"},
    )
    assert got.status_code == 401


def test_an_unknown_decision_is_a_404(client, db):
    token, _ = keys.mint(db, agent="ops")
    got = client.post(
        "/api/v1/decisions/nope/record",
        json={"letter": "a", "note": "x", "source": "terminal"},
        headers=auth(token),
    )
    assert got.status_code == 404


def test_the_route_is_in_the_published_contract(client, db):
    token, _ = keys.mint(db, agent="ops")
    paths = client.get("/api/v1/openapi.json", headers=auth(token)).json()["paths"]
    assert "/api/v1/decisions/{decision_id}/record" in paths


# --- what Jeremy sees -------------------------------------------------------


def test_the_gallery_marks_a_relayed_answer(client, db, asked):
    """He must be able to see that a decision was closed on his behalf, and by
    whom. That is what lets him contradict a relay he disagrees with — a relay
    he cannot audit is worse than an open question."""
    decisions.record_elsewhere(
        db,
        asked.id,
        letter="b",
        note="start with (b) but then (a) if we can",
        source="telegram",
        recorded_by="homelab-ops",
    )
    page = client.get(f"/a/{asked.artifact_id}").text
    assert "via telegram" in page
    assert "homelab-ops wrote it down" in page
    assert "not on the page" in page


def test_the_gallery_does_not_mark_an_answer_he_actually_gave(client, db, asked):
    """The control arm, and the one that makes the mark mean anything. If every
    entry carried a badge the exceptional one would be invisible."""
    decisions.answer(db, asked.id, letter="a", note="fine", by="jeremy")
    page = client.get(f"/a/{asked.artifact_id}").text
    assert 'class="relayed"' not in page
    assert "wrote it down" not in page


def test_a_relayed_answer_cannot_smuggle_markup_onto_the_gallery(client, db, asked):
    """`recorded_by` comes from a key and `source` from a fixed set, so neither
    should be able to carry markup — but the gallery origin holds Jeremy's
    session, so it is escaped here and asserted rather than reasoned about."""
    decisions.record_elsewhere(
        db,
        asked.id,
        letter="a",
        note="<img src=x onerror=alert(1)>",
        source="terminal",
        recorded_by="ops",
    )
    page = client.get(f"/a/{asked.artifact_id}").text
    # Assert it was ESCAPED, not that the word is absent. A bare "onerror" not
    # in page also passes when the note is missing entirely, and fails when the
    # note is correctly escaped and merely contains the word as text — which is
    # what happened the first time this was written.
    assert "&lt;img src=x onerror=alert(1)&gt;" in page, (
        "the note should be shown, escaped"
    )
    assert "<img" not in page, "and never as an element"
