"""A question addressed to a person other than the owner behaves right everywhere (people design,
slice 3a, docs/agent/specs/2026-11-24-people-design.md): a relay files under the person it is
addressed to, and the gallery draws answer controls and counts open questions only for the person
who may answer. Nothing sets `addressed_to` from a route yet, so these tests write it directly. A
second person is the `people.current` dependency overridden on the gallery app, with
`people.OPEN_SIGN_IN` flipped as in `test_people_answers.py`. What an agent reads is unchanged."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import decisions, formanswer, forms_ui, inbox, people, receipts, store
from ream.app import create_gallery_app

SAME = {"sec-fetch-site": "same-origin"}
FORM = """
<form data-artifact-form="rollout">
  <fieldset name="region"><legend>Region</legend>
    <label><input type="radio" name="region" value="a" required> eu</label>
    <label><input type="radio" name="region" value="b"> us</label>
  </fieldset>
  <label>Why <input type="text" name="why" maxlength="10"></label>
</form>
"""
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]
ANN = people.Person(id="ann", name="Ann", is_owner=False)


@pytest.fixture(autouse=True)
def _sign_in_open(monkeypatch):
    monkeypatch.setattr(people, "OPEN_SIGN_IN", True)


@pytest.fixture()
def owner_wall(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def ann_wall(db):
    other = create_gallery_app(db)
    other.dependency_overrides[people.current] = lambda: ANN
    return TestClient(other)


@pytest.fixture()
def form(db):
    page = store.publish(
        db,
        "ops",
        "Board",
        "<!doctype html><html><head><title>P</title></head><body>"
        + FORM
        + "</body></html>",
    )
    with db.connect() as conn:
        (did,) = conn.execute(
            "select id from decision where artifact_id = %s and kind = 'form'",
            (page.id,),
        ).fetchone()
    return page, did


@pytest.fixture()
def question(db):
    page = store.publish(db, "ops", "Q", "<p>x</p>")
    row = decisions.ask(
        db, agent="ops", artifact_id=page.id, question="which?", options=OPTIONS
    )
    return page, row


def _rows(db, sql, *params):
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]


def _address(db, decision_id, person):
    with db.connect() as conn:
        conn.execute(
            "update decision set addressed_to = %s where id = %s", (person, decision_id)
        )


def _submit(wall, page, values, version=1):
    return wall.post(
        f"/a/{page.id}/forms/rollout",
        json={"version": version, "values": values},
        headers=SAME,
    )


def _relay(db, did, **kw):
    return formanswer.relay(
        db,
        did,
        fields_given=kw.pop("fields_given", {"region": "b"}),
        note="said so",
        source="telegram",
        recorded_by="ops",
        **kw,
    )


# --- 1. a relay files under the person the question is for -----------------------------------


def test_a_relayed_answer_to_a_question_for_ann_is_hers(db, question):
    _, row = question
    _address(db, row.id, "ann")
    done = decisions.record_elsewhere(
        db, row.id, letter="a", note="n", source="telegram", recorded_by="ops"
    )
    assert done.answered_by == "ann"
    assert decisions.get_owned(db, "ops", row.id).answered_by == "ann"
    # the receipt written for it is about her answer
    receipts.declare_on(db, None, "ops", row.id, state="working", note="on it")
    assert _rows(db, "select person from receipt") == [("ann",)]


def test_a_relayed_answer_to_an_unaddressed_question_is_still_the_owners(db, question):
    _, row = question
    done = decisions.record_elsewhere(
        db, row.id, letter="a", note="n", source="telegram", recorded_by="ops"
    )
    assert done.answered_by == "jeremy"
    receipts.declare_on(db, None, "ops", row.id, state="working", note="on it")
    assert _rows(db, "select person from receipt") == [("jeremy",)]


def test_a_relay_on_a_form_for_ann_merges_into_her_row_not_the_owners(
    db, form, owner_wall, ann_wall
):
    page, did = form
    assert _submit(owner_wall, page, {"region": "a", "why": "mine"}).status_code == 200
    _address(db, did, "ann")
    done = _relay(db, did, fields_given={"region": "b", "why": "hers"})
    assert done["applied"] == ["region", "why"]
    got = {
        r[0]: r[1] for r in _rows(db, "select person, answer_record from form_answer")
    }
    assert got["jeremy"]["answers"]["region"] == "a"  # the owner's row is as it was
    assert got["ann"]["answers"] == {"region": "b", "why": "hers"}
    assert got["ann"]["answered_by"] == "ann"
    assert got["ann"]["history"][-1]["answered_by"] == "ann"
    assert _rows(db, "select answered_by from decision where id = %s", did) == [
        ("ann",)
    ]


def test_a_relay_on_an_unaddressed_form_still_files_under_the_owner(db, form):
    _, did = form
    _relay(db, did)
    assert [r[0] for r in _rows(db, "select person from form_answer")] == ["jeremy"]


def test_an_unapplied_relay_gives_her_row_her_own_answered_at_not_the_owners(
    db, form, owner_wall, ann_wall
):
    page, did = form
    _address(db, did, "ann")
    assert _submit(ann_wall, page, {"region": "a", "why": "hers"}).status_code == 200
    (hers,) = _rows(
        db,
        "select answered_at from form_answer where person = 'ann' and decision_id = %s",
        did,
    )
    # the owner answers a version of the question addressed to no one, later: the decision's
    # own answered_at and record are now his
    _address(db, did, None)
    assert _submit(owner_wall, page, {"region": "b", "why": "his"}).status_code == 200
    later = datetime.now(UTC) + timedelta(hours=1)
    with db.connect() as conn:
        conn.execute("update decision set answered_at = %s where id = %s", (later, did))
    _address(db, did, "ann")
    # a relay of hers that is older than her own submit changes nothing she said
    done = _relay(db, did, said_at=datetime.now(UTC) - timedelta(days=3))
    assert done["applied"] == []
    (row,) = _rows(
        db,
        "select answered_at from form_answer where person = 'ann' and decision_id = %s",
        did,
    )
    assert row == hers
    # and it left the decision's record, which is the owner's, alone
    ((record, by),) = _rows(
        db, "select answer_record, answered_by from decision where id = %s", did
    )
    assert by == "jeremy" and record["answered_by"] == "jeremy"


# --- 2. the gallery follows may_answer -------------------------------------------------------


def _has_buttons(wall, page, row_id):
    return f'action="/d/{row_id}/answer"' in wall.get(f"/a/{page.id}").text


def test_only_the_person_it_is_for_is_given_buttons_on_the_page(
    db, question, owner_wall, ann_wall
):
    page, row = question
    assert _has_buttons(owner_wall, page, row.id)
    assert not _has_buttons(ann_wall, page, row.id)  # unaddressed: the owner's
    _address(db, row.id, "ann")
    assert _has_buttons(ann_wall, page, row.id)
    assert not _has_buttons(owner_wall, page, row.id)
    assert "which?" not in owner_wall.get(f"/a/{page.id}").text


def test_an_answered_question_offers_no_change_to_someone_who_may_not_answer_it(
    db, question, owner_wall, ann_wall
):
    page, row = question
    _address(db, row.id, "ann")
    assert ann_wall.post(
        f"/d/{row.id}/answer",
        data={"letter": "a"},
        headers=SAME,
        follow_redirects=False,
    ).status_code in (200, 303)
    assert _has_buttons(ann_wall, page, row.id)  # she may change her mind
    seen = owner_wall.get(f"/a/{page.id}").text
    assert f'action="/d/{row.id}/answer"' not in seen
    assert "Tap a different letter" not in seen


def test_an_open_count_is_of_what_the_person_may_answer(db, question):
    _, row = question
    assert decisions.open_counts(db) == (0, 1, 1)
    assert decisions.open_counts(db, "jeremy") == (0, 1, 1)
    assert decisions.open_counts(db, "ann") == (0, 0, 0)
    _address(db, row.id, "ann")
    assert decisions.open_counts(db) == (0, 1, 1)  # the agent-side count is unchanged
    assert decisions.open_counts(db, "jeremy") == (0, 0, 0)
    assert decisions.open_counts(db, "ann") == (0, 1, 1)


def test_the_inbox_lists_and_counts_only_what_the_person_may_answer(
    db, question, owner_wall, ann_wall
):
    _page, row = question
    assert inbox.count(db, person="jeremy")[0] == 1
    assert inbox.count(db, person="ann")[0] == 0
    _address(db, row.id, "ann")
    assert inbox.count(db, person="jeremy")[0] == 0
    assert inbox.count(db, person="ann")[0] == 1
    assert [i.id for i in inbox.gather(db, person="ann")[0]] == [row.id]
    assert inbox.gather(db, person="jeremy")[0] == []
    assert f'action="/d/{row.id}/answer"' in ann_wall.get("/inbox").text
    assert f'action="/d/{row.id}/answer"' not in owner_wall.get("/inbox").text
    # the agent's own feed still sees it
    assert [r.decision.id for r in decisions.across_seats(db)] == [row.id]


def test_the_wall_says_waiting_only_to_the_person_who_may_answer(
    db, question, owner_wall, ann_wall
):
    _, row = question
    assert "1 question waiting on you" in owner_wall.get("/").text
    assert "waiting on you" not in ann_wall.get("/").text
    _address(db, row.id, "ann")
    assert "waiting on you" not in owner_wall.get("/").text
    assert "1 question waiting on you" in ann_wall.get("/").text
    assert "1 question for you" in ann_wall.get("/").text
    assert "question for you" not in owner_wall.get("/").text


def test_a_form_for_ann_gets_no_submit_bar_for_the_owner(db, form):
    page, did = form
    names = lambda who: [f["form"] for f in forms_ui.payload(db, page.id, who)["forms"]]
    assert names("jeremy") == ["rollout"] and names("ann") == []
    _address(db, did, "ann")
    assert names("jeremy") == [] and names("ann") == ["rollout"]
