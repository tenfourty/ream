"""Review rounds (page review step 4, homelab-ij8.108): a republish after
changes requested opens the next round by itself, a verdict closes the round,
and a verdict meant for an earlier round is refused."""

import threading
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from artifacts import comments, decisions, keys, nudge, store
from artifacts.app import create_gallery_app

PAGE = (
    "<!doctype html><title>Plan</title>"
    "<p>Rotate the keys every 90 days.</p><p>Email the team when done.</p>"
)
RANGE = {"kind": "range", "path": "/p[1]", "start": 0, "end": 6}
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(nudge, "emit", lambda **kw: None)


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def _page(db, agent="seat"):
    return store.publish(db, agent, "Plan", PAGE)


def _republish(db, page, body, agent="seat"):
    """Publish `body` as the page's next version, from whatever is live."""
    with db.connect() as conn:
        revision = conn.execute(
            "select v.revision from artifact a join version v"
            " on v.artifact_id = a.id and v.n = a.current_version where a.id = %s",
            (page.id,),
        ).fetchone()[0]
    return store.patch(db, agent, page.id, body, revision)


def _v(k):
    return PAGE.replace("90 days", f"{90 + k} days")


def _rounds(db, review_id):
    with db.connect() as conn:
        return conn.execute(
            "select n, version_n, base_version, closed_at is not null, verdict"
            " from review_round where review_id = %s order by n",
            (review_id,),
        ).fetchall()


def _mark(db, page, **kw):
    return comments.add(
        db,
        artifact_id=page.id,
        author_kind="human",
        author="jeremy",
        kind="remove",
        anchor=RANGE,
        quote="Rotate",
        body="",
        **kw,
    )


def _auth(db, agent="seat"):
    token, _ = keys.mint(db, agent=agent)
    return {"Authorization": f"Bearer {token}"}


def _sent_back(db):
    """A review in round 1 at v1, sent back with changes requested."""
    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    decisions.answer(db, review.id, letter="c", note="tighten it", by="jeremy")
    return page, review


# ---- open ----


def test_asking_opens_round_one_at_the_current_version(db):
    page = _page(db)
    _republish(db, page, _v(1))
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    assert _rounds(db, review.id) == [(1, 2, None, False, None)]
    with db.connect() as conn:
        current = decisions.current_round(conn, review.id)
    assert (current.n, current.version_n, current.base_version) == (1, 2, None)
    assert current.closed_at is None and current.verdict is None


def test_migrating_gives_a_review_from_before_rounds_its_round_one(db):
    page, review = _sent_back(db)
    open_one = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    with db.connect() as conn:
        conn.execute("delete from review_round")
    db.migrate()
    db.migrate()  # idempotent
    assert _rounds(db, review.id) == [(1, 1, None, True, "changes_requested")]
    assert _rounds(db, open_one.id) == [(1, 1, None, False, None)]


def test_a_verdict_closes_the_round(db):
    _page, review = _sent_back(db)
    assert _rounds(db, review.id) == [(1, 1, None, True, "changes_requested")]


def test_a_relayed_verdict_closes_the_round_too(db):
    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    decisions.record_elsewhere(
        db,
        review.id,
        letter="a",
        note="ship it",
        source="terminal",
        recorded_by="seat",
    )
    assert _rounds(db, review.id) == [(1, 1, None, True, "approved")]


def test_changes_requested_then_a_republish_opens_round_two(db):
    page, review = _sent_back(db)
    with db.connect() as conn:
        conn.execute(
            "update decision set acknowledged_at = %s, acknowledged_by = 'seat'"
            " where id = %s",
            (datetime.now(UTC), review.id),
        )
    published = _republish(db, page, _v(1))
    assert published.version == 2
    assert _rounds(db, review.id) == [
        (1, 1, None, True, "changes_requested"),
        (2, 2, 1, False, None),
    ]
    row = decisions.get_owned(db, "seat", review.id)
    assert row.answer is None and row.answer_note == ""
    assert row.answered_at is None and row.answered_by is None
    assert row.answer_source is None and row.recorded_by is None
    assert row.said_at is None and row.acknowledged_at is None
    assert row.closed_at is None


def test_approved_then_a_republish_opens_nothing(db):
    for letter in ("a", "b", "d"):
        page = _page(db)
        review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
        decisions.answer(db, review.id, letter=letter, by="jeremy")
        _republish(db, page, _v(1))
        assert len(_rounds(db, review.id)) == 1, letter
        assert decisions.get_owned(db, "seat", review.id).answer == letter


def test_an_identical_republish_opens_nothing(db):
    page, review = _sent_back(db)
    unchanged = _republish(db, page, PAGE)
    assert unchanged.unchanged
    assert len(_rounds(db, review.id)) == 1
    assert decisions.get_owned(db, "seat", review.id).answer == "c"


def test_a_closed_review_opens_nothing(db):
    page, review = _sent_back(db)
    decisions.close(db, "seat", review.id)
    _republish(db, page, _v(1))
    assert len(_rounds(db, review.id)) == 1


def test_three_republishes_in_round_two_keep_its_base(db):
    page, review = _sent_back(db)
    for k in (1, 2, 3):
        _republish(db, page, _v(k))
    assert _rounds(db, review.id) == [
        (1, 1, None, True, "changes_requested"),
        (2, 4, 1, False, None),
    ]
    _republish(db, page, _v(3))  # identical: no version, no move
    assert _rounds(db, review.id)[-1] == (2, 4, 1, False, None)


def test_round_three_takes_round_twos_version_as_its_base(db):
    page, review = _sent_back(db)
    _republish(db, page, _v(1))
    _republish(db, page, _v(2))
    decisions.answer(db, review.id, letter="c", by="jeremy", round=2)
    _republish(db, page, _v(3))
    assert _rounds(db, review.id)[-1] == (3, 4, 3, False, None)


# ---- refuse ----


def test_a_second_verdict_on_a_round_is_refused_naming_the_round(db):
    _page, review = _sent_back(db)
    with pytest.raises(decisions.AlreadyDecided) as err:
        decisions.answer(db, review.id, letter="a", by="jeremy")
    assert err.value.round.n == 1
    assert err.value.round.verdict == "changes_requested"


def test_a_verdict_for_a_stale_round_is_refused(db):
    page, review = _sent_back(db)
    _republish(db, page, _v(1))
    with pytest.raises(decisions.AlreadyDecided) as err:
        decisions.answer(db, review.id, letter="a", by="jeremy", round=1)
    assert err.value.round.n == 1
    assert err.value.current.n == 2
    assert decisions.get_owned(db, "seat", review.id).answer is None
    assert _rounds(db, review.id)[-1] == (2, 2, 1, False, None)
    # The current round's own form is accepted.
    decisions.answer(db, review.id, letter="a", by="jeremy", round=2)
    assert _rounds(db, review.id)[-1] == (2, 2, 1, True, "approved")


def test_a_relay_for_a_stale_round_is_refused(db):
    page, review = _sent_back(db)
    _republish(db, page, _v(1))
    with pytest.raises(decisions.AlreadyDecided):
        decisions.record_elsewhere(
            db,
            review.id,
            letter="a",
            note="ok",
            source="terminal",
            recorded_by="seat",
            round=1,
        )
    assert decisions.get_owned(db, "seat", review.id).answer is None


def test_the_form_carries_its_round(db, client):
    page, _review = _sent_back(db)
    _republish(db, page, _v(1))
    html = client.get(f"/a/{page.id}").text
    assert '<input type="hidden" name="round" value="2">' in html


def test_a_stale_tab_gets_409_naming_the_current_round(db, client):
    page, review = _sent_back(db)
    _republish(db, page, _v(1))
    stale = client.post(
        f"/d/{review.id}/answer",
        data={"letter": "a", "round": "1"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert stale.status_code == 409
    assert "This review already has a verdict for round 1:" in stale.text
    assert "Changes requested" in stale.text
    assert "round 2" in stale.text
    assert decisions.get_owned(db, "seat", review.id).answer is None


def test_a_second_tap_on_the_same_round_names_the_round(db, client):
    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    for letter, status in (("a", 303), ("c", 409)):
        r = client.post(
            f"/d/{review.id}/answer",
            data={"letter": letter, "round": "1"},
            headers=SAME_ORIGIN,
            follow_redirects=False,
        )
        assert r.status_code == status
    assert "This review already has a verdict for round 1:" in r.text
    assert "Approved" in r.text


def test_the_api_relay_takes_a_round_and_the_review_reports_it(db, client):
    h = _auth(db)
    page, review = _sent_back(db)
    assert client.get(f"/api/v1/reviews/{review.id}", headers=h).json()["round"] == 1
    _republish(db, page, _v(1))
    assert client.get(f"/api/v1/reviews/{review.id}", headers=h).json()["round"] == 2
    body = {"letter": "a", "note": "ok", "source": "terminal", "round": 1}
    stale = client.post(f"/api/v1/decisions/{review.id}/record", headers=h, json=body)
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["round"] == 2 and detail["verdict_round"] == 1
    assert detail["verdict"] == "changes_requested"
    ok = client.post(
        f"/api/v1/decisions/{review.id}/record", headers=h, json={**body, "round": 2}
    )
    assert ok.status_code == 200, ok.text


# ---- marks ----


def test_a_mark_made_in_round_two_is_stamped_round_two(db):
    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="live")
    first = _mark(db, page)
    decisions.answer(db, review.id, letter="c", by="jeremy")
    between = _mark(db, page)
    _republish(db, page, _v(1))
    second = _mark(db, page)
    with db.connect() as conn:
        stamps = dict(
            conn.execute(
                "select id, review_round from comment where id = any(%s)",
                ([first.id, between.id, second.id],),
            ).fetchall()
        )
    assert stamps == {first.id: 1, between.id: None, second.id: 2}


def test_held_mode_carries_over_into_round_two(db):
    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id, mode="held")
    held_in_one = _mark(db, page)
    assert held_in_one.held
    decisions.answer(db, review.id, letter="c", by="jeremy")
    _republish(db, page, _v(1))
    assert decisions.get_owned(db, "seat", review.id).review_mode == "held"
    held_in_two = _mark(db, page)
    assert held_in_two.held, "a mark in round 2 of a held review is held"
    flags = {
        c.id: c.held for c in comments.list_for_artifact(db, page.id, include_held=True)
    }
    assert flags == {held_in_one.id: False, held_in_two.id: True}
    decisions.answer(db, review.id, letter="a", by="jeremy", round=2)
    assert decisions.get_owned(db, "seat", review.id).answer == "b"


# ---- the race: a republish and a verdict on round 1 at the same moment ----


def _until_waiting_on_a_lock(db, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db.connect() as conn:
            (waiting,) = conn.execute(
                "select count(*) from pg_stat_activity"
                " where datname = current_database() and wait_event_type = 'Lock'"
            ).fetchone()
        if waiting:
            return
        time.sleep(0.005)
    raise AssertionError("no statement ever waited on a lock")


def _gate_after(monkeypatch, module, name):
    reached, go = threading.Event(), threading.Event()
    real = getattr(module, name)

    def wrapped(*args, **kwargs):
        out = real(*args, **kwargs)
        reached.set()
        assert go.wait(10)
        return out

    monkeypatch.setattr(module, name, wrapped)
    return reached, go


@pytest.mark.parametrize("letter", ["c", "a"])
@pytest.mark.postgres_only  # waits for a lock through pg_stat_activity
def test_a_verdict_holding_the_lock_lands_on_round_one(db, monkeypatch, letter):
    """The verdict locks the review first; the republish waits, then sees the
    verdict and opens round 2 only if it was changes requested."""
    from artifacts import holds

    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    reached, go = _gate_after(monkeypatch, holds, "approved_letter")
    out = {}
    verdict = threading.Thread(
        target=lambda: out.update(
            v=decisions.answer(db, review.id, letter=letter, by="jeremy", round=1)
        )
    )
    verdict.start()
    assert reached.wait(10)
    republish = threading.Thread(
        target=lambda: out.update(p=_republish(db, page, _v(1)))
    )
    republish.start()
    _until_waiting_on_a_lock(db)
    assert republish.is_alive(), "the republish waits for the verdict's lock"
    go.set()
    verdict.join(20)
    republish.join(20)
    assert out["v"].answer == letter and out["p"].version == 2
    rounds = _rounds(db, review.id)
    assert rounds[0][:2] == (1, 1) and rounds[0][3] is True
    if letter == "c":
        assert rounds[1] == (2, 2, 1, False, None)
        assert decisions.get_owned(db, "seat", review.id).answer is None
    else:
        assert len(rounds) == 1
        assert decisions.get_owned(db, "seat", review.id).answer == "a"


@pytest.mark.postgres_only  # waits for a lock through pg_stat_activity
def test_a_republish_holding_the_lock_opens_nothing_and_the_verdict_counts(
    db, monkeypatch
):
    """The republish locks the review first while round 1 has no verdict: it
    opens nothing, and the verdict that waited lands on round 1."""
    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    reached, go = _gate_after(monkeypatch, decisions, "_advance_rounds")
    out = {}
    republish = threading.Thread(
        target=lambda: out.update(p=_republish(db, page, _v(1)))
    )
    republish.start()
    assert reached.wait(10)
    verdict = threading.Thread(
        target=lambda: out.update(
            v=decisions.answer(db, review.id, letter="c", by="jeremy", round=1)
        )
    )
    verdict.start()
    _until_waiting_on_a_lock(db)
    assert verdict.is_alive(), "the verdict waits for the republish's lock"
    go.set()
    republish.join(20)
    verdict.join(20)
    assert out["p"].version == 2 and out["v"].answer == "c"
    # Round 1 moved to the version he was looking at when he answered, and
    # carries the verdict; nothing opened.
    assert _rounds(db, review.id) == [(1, 2, None, True, "changes_requested")]


# ---- review fixes: base version, cached forms, old pods, forged rounds ----


def test_the_base_is_the_version_he_was_shown_not_the_latest(db):
    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    _republish(db, page, _v(1))  # v2 exists; his tab still shows v1
    decisions.answer(db, review.id, letter="c", by="jeremy", round=1, version=1)
    _republish(db, page, _v(2))  # v3
    assert _rounds(db, review.id)[-1] == (2, 3, 1, False, None)


def test_a_relay_without_a_version_bases_on_the_rounds_version(db):
    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    _republish(db, page, _v(1))
    decisions.record_elsewhere(
        db, review.id, letter="c", note="redo", source="terminal", recorded_by="seat"
    )
    _republish(db, page, _v(2))
    assert _rounds(db, review.id)[-1] == (2, 3, 2, False, None)


def test_the_form_carries_the_version_it_showed(db, client):
    page = _page(db)
    decisions.ask_review(db, agent="seat", artifact_id=page.id)
    _republish(db, page, _v(1))
    html = client.get(f"/a/{page.id}").text
    assert '<input type="hidden" name="version" value="2">' in html


def test_a_tapped_verdict_records_the_version_from_the_form(db, client):
    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    _republish(db, page, _v(1))
    r = client.post(
        f"/d/{review.id}/answer",
        data={"letter": "c", "round": "1", "version": "1"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303
    _republish(db, page, _v(2))
    assert _rounds(db, review.id)[-1] == (2, 3, 1, False, None)


def test_a_form_cached_before_rounds_counts_as_round_one(db, client):
    page, review = _sent_back(db)
    _republish(db, page, _v(1))
    old_form = client.post(
        f"/d/{review.id}/answer",
        data={"letter": "a"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert old_form.status_code == 409
    assert "round 2" in old_form.text
    assert decisions.get_owned(db, "seat", review.id).answer is None


def test_a_relay_without_a_round_takes_the_current_one(db, client):
    h = _auth(db)
    page, review = _sent_back(db)
    _republish(db, page, _v(1))
    body = {"letter": "a", "note": "ok", "source": "terminal"}
    ok = client.post(f"/api/v1/decisions/{review.id}/record", headers=h, json=body)
    assert ok.status_code == 200, ok.text
    assert _rounds(db, review.id)[-1] == (2, 2, 1, True, "approved")


def test_a_verdict_from_an_old_pod_is_closed_before_the_next_round(db):
    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    with db.connect() as conn:  # an old pod: answers, leaves round 1 open
        conn.execute(
            "update decision set answer = 'c', answered_at = %s,"
            " answered_by = 'jeremy', answer_source = 'page' where id = %s",
            (datetime.now(UTC), review.id),
        )
    _republish(db, page, _v(1))
    rounds = _rounds(db, review.id)
    assert rounds[0] == (1, 1, None, True, "changes_requested")
    assert rounds[1] == (2, 2, 1, False, None)
    assert decisions.get_owned(db, "seat", review.id).answer is None


def test_an_old_pods_approval_opens_nothing_and_closes_round_one(db):
    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    with db.connect() as conn:
        conn.execute(
            "update decision set answer = 'a', answered_at = %s,"
            " answered_by = 'jeremy', answer_source = 'page' where id = %s",
            (datetime.now(UTC), review.id),
        )
    _republish(db, page, _v(1))
    assert _rounds(db, review.id) == [(1, 1, None, True, "approved")]


def test_a_forged_later_round_is_told_the_current_round(db, client):
    page = _page(db)
    review = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    r = client.post(
        f"/d/{review.id}/answer",
        data={"letter": "a", "round": "5"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 409
    assert "now in round 1" in r.text
    assert "agent closed" not in r.text


def test_the_review_reports_when_its_round_opened(db, client):
    h = _auth(db)
    page, review = _sent_back(db)
    first = client.get(f"/api/v1/reviews/{review.id}", headers=h).json()
    _republish(db, page, _v(1))
    second = client.get(f"/api/v1/reviews/{review.id}", headers=h).json()
    assert first["round_opened_at"] and second["round_opened_at"]
    assert second["round_opened_at"] > first["round_opened_at"]
