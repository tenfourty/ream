"""Deleting a page, and the thirty days in which it is still there.

homelab-ij8.84.8. Jeremy, 20 Sep 2026, answering on his own page: (a) "Delete
it, but keep it recoverable for thirty days -- plus a separate destroy-it-now",
and in chat immediately after: "ideally the user and the agent can both recover
deleted pages".

THE CLAIM THIS FILE EXISTS TO DEFEND: a delete destroys nothing. The body, every
version, and every decision with his answers and his notes are all still there
for the window. Deleting is the cheap way to tidy a gallery and it must not be
the way the record of what he decided is destroyed -- homelab-ij8.44's lesson,
which bit twice in one day.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>B</title></head><body><p>x</p></body></html>"
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db), base_url="https://testserver")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _answered(db, artifact_id):
    asked = decisions.ask(
        db,
        agent="seat",
        artifact_id=artifact_id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )
    decisions.answer(db, asked.id, letter="a", note="his actual words", by="jeremy")
    return asked


def _aged(db, artifact_id, days):
    with db.connect() as conn:
        conn.execute(
            "update artifact set deleted_at = %s where id = %s",
            (datetime.now(UTC) - timedelta(days=days), artifact_id),
        )


# --- a delete destroys nothing -----------------------------------------------


def test_everything_he_said_survives_a_delete(db):
    """THE ONE THAT MATTERS."""
    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    _answered(db, page.id)
    store.patch(db, "seat", page.id, PAGE.replace("x", "y"), page.revision)

    assert store.set_deleted(db, page.id, True, agent="seat")

    kept = decisions.list_for_artifact(db, page.id, include_answered=True)
    assert len(kept) == 1
    assert kept[0].answer == "a"
    assert kept[0].answer_note == "his actual words"
    with db.connect() as conn:
        versions = conn.execute(
            "select count(*) from version where artifact_id = %s", (page.id,)
        ).fetchone()[0]
    assert versions == 2, "every version is still there"


def test_a_deleted_page_leaves_every_listing(client, db):
    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    store.set_deleted(db, page.id, True, agent="seat")

    assert store.list_for_agent(db, "seat") == []
    assert page.id not in client.get("/").text
    with pytest.raises(store.NotFound):
        store.get_owned(db, "seat", page.id)
    with pytest.raises(store.NotFound):
        store.get_body(db, "seat", page.id)


def test_a_deleted_pages_address_stops_working(db):
    """Otherwise "deleted" is only a change to a listing, and anyone holding
    the link would never know."""
    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    assert store.read_token(db, page.id) is not None
    store.set_deleted(db, page.id, True, agent="seat")
    assert store.read_token(db, page.id) is None


def test_housekeeping_cannot_touch_a_deleted_page(db):
    """Archive, rename, pin and duplicate all resolve through the same scoping
    clause, so one rule covers them -- which is the point of there being one."""
    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    store.set_deleted(db, page.id, True, agent="seat")

    assert store.rename(db, page.id, "New name", agent="seat") is False
    assert store.set_archived(db, page.id, True, agent="seat") is False
    assert store.set_pinned(db, page.id, True, agent="seat") is False
    with pytest.raises(store.NotFound):
        store.duplicate(db, page.id, agent="seat")


# --- recovery, by either of them ---------------------------------------------


def test_the_owning_agent_lists_and_recovers_its_own(client, db):
    """The clause that shapes the build. An agent deletes a page in error as
    readily as a human does, and it is usually the agent that notices --
    within seconds, and while Jeremy is asleep."""
    token, _ = keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    client.delete(f"/api/v1/artifacts/{page.id}", headers=auth(token))

    listed = client.get("/api/v1/artifacts/deleted", headers=auth(token)).json()
    assert [r["id"] for r in listed] == [page.id]
    assert listed[0]["deleted_at"] is not None

    back = client.post(f"/api/v1/artifacts/{page.id}/restore", headers=auth(token))
    assert back.status_code == 200
    assert store.get_owned(db, "seat", page.id).title == "Board"
    assert client.get("/api/v1/artifacts/deleted", headers=auth(token)).json() == []


def test_he_recovers_it_from_the_gallery(client, db):
    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    store.set_deleted(db, page.id, True, agent=None)

    bin_view = client.get("/?filter=deleted").text
    assert "Board" in bin_view
    assert "days left" in bin_view

    out = client.post(
        f"/a/{page.id}/restore", headers=SAME_ORIGIN, follow_redirects=False
    )
    assert out.status_code == 303
    assert store.get_owned(db, "seat", page.id).title == "Board"


def test_restoring_keeps_the_address_and_the_history(db):
    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    store.patch(db, "seat", page.id, PAGE.replace("x", "y"), page.revision)
    _answered(db, page.id)

    store.set_deleted(db, page.id, True, agent="seat")
    store.set_deleted(db, page.id, False, agent="seat")

    row = store.get_owned(db, "seat", page.id)
    assert row.id == page.id, "the same address"
    assert row.current_version == 2
    assert len(decisions.list_for_artifact(db, page.id, include_answered=True)) == 1


def test_a_seat_cannot_delete_or_recover_another_seats_page(client, db):
    keys.mint(db, agent="other")
    intruder, _ = keys.mint(db, agent="intruder")
    theirs = store.publish(db, "other", "Not yours", PAGE)

    assert (
        client.delete(
            f"/api/v1/artifacts/{theirs.id}", headers=auth(intruder)
        ).status_code
        == 404
    )
    assert store.get_owned(db, "other", theirs.id).title == "Not yours"

    store.set_deleted(db, theirs.id, True, agent="other")
    assert client.get("/api/v1/artifacts/deleted", headers=auth(intruder)).json() == []
    assert (
        client.post(
            f"/api/v1/artifacts/{theirs.id}/restore", headers=auth(intruder)
        ).status_code
        == 404
    )


def test_the_deleted_listing_is_soonest_to_go_first(db):
    """The opposite order to every other listing here, and right for the same
    reason they are wrong for this: the question is not "what did I just
    delete" but "what am I about to lose"."""
    keys.mint(db, agent="seat")
    old = store.publish(db, "seat", "Oldest", PAGE)
    mid = store.publish(db, "seat", "Middle", PAGE)
    new = store.publish(db, "seat", "Newest", PAGE)
    for page in (old, mid, new):
        store.set_deleted(db, page.id, True, agent="seat")
    _aged(db, old.id, 29)
    _aged(db, mid.id, 10)
    _aged(db, new.id, 1)
    assert [r.title for r in store.list_deleted(db, "seat")] == [
        "Oldest",
        "Middle",
        "Newest",
    ]


# --- destroy -----------------------------------------------------------------


def test_destroy_takes_everything_and_needs_the_word(client, db):
    token, _ = keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    _answered(db, page.id)
    store.set_deleted(db, page.id, True, agent="seat")

    refused = client.post(
        f"/api/v1/artifacts/{page.id}/destroy",
        json={"confirm": "yes"},
        headers=auth(token),
    )
    assert refused.status_code == 422
    assert store.list_deleted(db, "seat") != [], "and it is still there"

    gone = client.post(
        f"/api/v1/artifacts/{page.id}/destroy",
        json={"confirm": "destroy"},
        headers=auth(token),
    )
    assert gone.status_code == 200
    assert store.list_deleted(db, "seat") == []
    with db.connect() as conn:
        assert (
            conn.execute(
                "select count(*) from version where artifact_id = %s", (page.id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "select count(*) from decision where artifact_id = %s", (page.id,)
            ).fetchone()[0]
            == 0
        )


def test_the_gallery_destroy_needs_the_word_typed(client, db):
    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    store.set_deleted(db, page.id, True, agent=None)

    refused = client.post(
        f"/a/{page.id}/destroy",
        data={"confirm": ""},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert refused.status_code == 400
    assert store.list_deleted(db) != []

    out = client.post(
        f"/a/{page.id}/destroy",
        data={"confirm": "destroy"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert out.status_code == 303
    assert store.list_deleted(db) == []


def test_a_cross_site_form_can_neither_delete_nor_destroy(client, db):
    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Board", PAGE)
    for route, data in (("delete", {}), ("destroy", {"confirm": "destroy"})):
        out = client.post(
            f"/a/{page.id}/{route}",
            data=data,
            headers={"sec-fetch-site": "cross-site"},
            follow_redirects=False,
        )
        assert out.status_code == 403
    assert store.get_owned(db, "seat", page.id).title == "Board"


# --- and the thirty days actually happens ------------------------------------


def test_the_sweep_destroys_what_is_past_the_window_and_nothing_else(db):
    """A soft delete with no reaper is not a thirty-day window, it is a hidden
    row for ever -- and he chose this partly because it is honest about
    eventually being gone."""
    keys.mint(db, agent="seat")
    expired = store.publish(db, "seat", "Expired", PAGE)
    fresh = store.publish(db, "seat", "Fresh", PAGE)
    live = store.publish(db, "seat", "Live", PAGE)
    store.set_deleted(db, expired.id, True, agent="seat")
    store.set_deleted(db, fresh.id, True, agent="seat")
    _aged(db, expired.id, 31)
    _aged(db, fresh.id, 29)

    gone = store.purge_expired(db)

    assert gone == [expired.id], "it reports WHICH, not how many"
    assert [r.title for r in store.list_deleted(db, "seat")] == ["Fresh"]
    assert store.get_owned(db, "seat", live.id).title == "Live", "a live page is safe"


def test_the_sweep_is_reachable_as_a_command(db, monkeypatch, capsys):
    """Built, tested and called by nothing is the failure this command exists
    not to repeat -- the uncollected-answer alert in this same service is
    exactly that. So the command is driven here, not just the function."""
    from artifacts import admin

    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Expired", PAGE)
    store.set_deleted(db, page.id, True, agent="seat")
    _aged(db, page.id, 31)
    monkeypatch.setattr(admin, "_database_url", lambda: db.dsn)

    admin.main(["purge-deleted", "--dry-run"])
    rehearsal = capsys.readouterr().out
    assert f"would destroy {page.id}" in rehearsal
    assert "1 due" in rehearsal
    assert store.list_deleted(db, "seat") != [], "a dry run removes nothing"

    admin.main(["purge-deleted"])
    done = capsys.readouterr().out
    assert f"destroyed {page.id}" in done
    assert "1 destroyed" in done
    assert store.list_deleted(db, "seat") == []


# --- a question on a page that gets tidied away ------------------------------


def test_a_question_does_not_go_silent_when_its_page_is_tidied_away(client, db):
    """FOUND BY USING THE INTERFACE, not by reading it.

    Archive or delete a page holding an unanswered question and, before this,
    three things were true at once: the question stayed open, the agent that
    asked was still waiting on it, and the gallery stopped mentioning it
    entirely. So he would never answer and the agent would never unblock --
    a silent dead end in the one thing this service exists to do.

    The count is kept SEPARATE from "waiting on you" rather than added to it:
    "3 questions waiting" leading to a wall that shows two would be a worse lie
    than the silence it replaced.
    """
    keys.mint(db, agent="seat")
    for tidy in (
        lambda i: store.set_archived(db, i, True, agent=None),
        lambda i: store.set_deleted(db, i, True, agent=None),
    ):
        page = store.publish(db, "seat", "Asks something", PAGE)
        _ask(db, page.id)
        assert "waiting on you" in client.get("/").text

        tidy(page.id)

        assert decisions.list_for_artifact(db, page.id), "the question is still open"
        wall = client.get("/").text
        assert "still unanswered" in wall, (
            "the gallery went silent about a question he has not answered"
        )
        store.destroy(db, page.id, agent=None)


def test_a_deleted_page_shows_what_is_unanswered_on_it(client, db):
    """The archived fold has always shown this; the bin did not, so a deleted
    page holding a question was the one place in the gallery where that fact
    was completely invisible."""
    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Asks something", PAGE)
    _ask(db, page.id)
    store.set_deleted(db, page.id, True, agent=None)

    assert "1 question unanswered" in client.get("/?filter=deleted").text


def test_the_tidied_count_is_not_folded_into_the_waiting_count(client, db):
    """Two different facts, two different numbers, and neither inflated."""
    keys.mint(db, agent="seat")
    live = store.publish(db, "seat", "Live", PAGE)
    gone = store.publish(db, "seat", "Gone", PAGE)
    _ask(db, live.id)
    _ask(db, gone.id)
    store.set_archived(db, gone.id, True, agent=None)

    wall = client.get("/").text
    assert "1 question waiting on you" in wall, "the wall's own count is of the wall"
    assert "1 question tidied away" in wall
    assert "2 questions waiting on you" not in wall


def _ask(db, artifact_id):
    return decisions.ask(
        db,
        agent="seat",
        artifact_id=artifact_id,
        question="Which?",
        options=[{"letter": "a", "label": "A"}, {"letter": "b", "label": "B"}],
        recommended="a",
    )


# --- and the agent that asked is told ----------------------------------------


def test_the_asking_agent_hears_when_its_question_leaves_the_wall(db):
    """THE POINT OF homelab-ij8.84.10. Before this, an agent that armed a waiter
    on a decision whose page was then archived waited until its own timeout,
    every time, with no way to tell "not answered yet" from "he is never going
    to be shown this". A seat can lose a shift to that.
    """
    from artifacts import events

    keys.mint(db, agent="seat")
    for what, tidy in (
        ("archived", lambda i: store.set_archived(db, i, True, agent="seat")),
        ("deleted", lambda i: store.set_deleted(db, i, True, agent="seat")),
    ):
        page = store.publish(db, "seat", f"Asks ({what})", PAGE)
        asked = _ask(db, page.id)
        before = events.list_for_agent(db, "seat")
        cursor = before[-1].seq if before else 0

        tidy(page.id)

        new = events.list_for_agent(db, "seat", since=cursor)
        shelved = [e for e in new if e.kind == "decision_shelved"]
        assert len(shelved) == 1, (
            f"{what}: expected one event, got {[e.kind for e in new]}"
        )
        assert shelved[0].ref_id == asked.id, "it names WHICH question"
        assert what in shelved[0].summary
        assert "still unanswered" in shelved[0].note
        assert "Stop waiting" in shelved[0].note, "it says what to do about it"


def test_a_page_with_nothing_open_produces_no_noise(db):
    """An event per archive regardless would train agents to ignore the feed."""
    from artifacts import events

    keys.mint(db, agent="seat")
    quiet = store.publish(db, "seat", "Nothing open", PAGE)
    answered = store.publish(db, "seat", "Already answered", PAGE)
    asked = _ask(db, answered.id)
    decisions.answer(db, asked.id, letter="a", note="", by="jeremy")

    before = len(events.list_for_agent(db, "seat"))
    store.set_archived(db, quiet.id, True, agent="seat")
    store.set_archived(db, answered.id, True, agent="seat")
    after = events.list_for_agent(db, "seat")

    assert [e for e in after[before:] if e.kind == "decision_shelved"] == []


def test_putting_the_page_back_does_not_announce_anything(db):
    """Unarchiving is not news of the same kind -- the question simply becomes
    visible again, and an agent polling its decisions will see it. Firing an
    event here would mean two events per round trip and nothing to do with the
    second."""
    from artifacts import events

    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Round trip", PAGE)
    _ask(db, page.id)
    store.set_archived(db, page.id, True, agent="seat")
    mark = len(events.list_for_agent(db, "seat"))

    store.set_archived(db, page.id, False, agent="seat")

    assert len(events.list_for_agent(db, "seat")) == mark


def test_the_event_and_the_archive_commit_together(db):
    """The event is written on the caller's own connection, so a failed archive
    cannot leave an agent told about something that did not happen."""
    from artifacts import events

    keys.mint(db, agent="seat")
    page = store.publish(db, "seat", "Asks", PAGE)
    _ask(db, page.id)

    # An archive that matches nothing must produce no event at all.
    before = len(events.list_for_agent(db, "seat"))
    assert store.set_archived(db, "nosuchpage", True, agent="seat") is False
    assert len(events.list_for_agent(db, "seat")) == before
