"""An answer and a receipt are one person's (people design, slice 3;
docs/agent/specs/2026-11-24-people-design.md). Nobody but the owner can sign in yet, so a second
person here is the `people.current` dependency overridden on the gallery app, with
`people.OPEN_SIGN_IN` flipped the way step 4 will. What an agent reads over `/api/v1` is unchanged:
`answer_record` is still the one record, and `addressed_to` is never sent."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import decisions, formanswer, people, receipts, store
from ream.app import create_gallery_app
from ream.db import jsonb

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
    (found,) = (d for d in _decisions(db, page.id) if d["kind"] == "form")
    return page, found["id"]


def _decisions(db, artifact_id):
    with db.connect() as conn:
        return [
            {"id": r[0], "kind": r[1]}
            for r in conn.execute(
                "select id, kind from decision where artifact_id = %s", (artifact_id,)
            ).fetchall()
        ]


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


def test_may_answer_is_the_person_it_is_addressed_to_else_the_owners(db):
    owner = people.owner_person()
    row = decisions.ask(
        db,
        agent="ops",
        artifact_id=store.publish(db, "ops", "T", "<p>x</p>").id,
        question="q?",
        options=OPTIONS,
    )
    assert row.addressed_to is None
    assert decisions.may_answer(owner, row) and not decisions.may_answer(ANN, row)
    ann_s = decisions.DecisionRow(**{**row.__dict__, "addressed_to": "ann"})
    assert decisions.may_answer(ANN, ann_s) and not decisions.may_answer(owner, ann_s)


def test_an_agent_never_reads_whom_a_question_is_addressed_to(db):
    page = store.publish(db, "ops", "T", "<p>x</p>")
    row = decisions.ask(
        db, agent="ops", artifact_id=page.id, question="q?", options=OPTIONS
    )
    _address(db, row.id, "ann")
    assert "addressed_to" not in decisions.public(
        decisions.get_owned(db, "ops", row.id)
    )


def test_each_person_keeps_their_own_record_of_a_form(db, form, owner_wall):
    page, did = form
    _address(db, did, None)
    assert _submit(owner_wall, page, {"region": "a", "why": "mine"}).status_code == 200
    formanswer.submit(db, did, version=1, values={"region": "b"}, by="ann")
    got = {
        r[0]: r[1] for r in _rows(db, "select person, answer_record from form_answer")
    }
    assert set(got) == {"jeremy", "ann"}
    assert (
        got["jeremy"]["answers"]["region"] == "a"
        and got["jeremy"]["answers"]["why"] == "mine"
    )
    # Ann's record starts from nothing: it does not take Jeremy's answers
    assert got["ann"]["answers"]["region"] == "b" and got["ann"]["answers"]["why"] == ""
    assert len(got["jeremy"]["history"]) == 1 and len(got["ann"]["history"]) == 1
    # a second submit by Jeremy merges with his own, not with Ann's
    formanswer.submit(db, did, version=1, values={"region": "a"}, by="jeremy")
    mine = _rows(db, "select answer_record from form_answer where person = 'jeremy'")[
        0
    ][0]
    assert mine["answers"]["why"] == "mine" and len(mine["history"]) == 2


def test_the_decisions_own_record_is_the_one_an_agent_reads_and_follows_the_owner(
    db, form, owner_wall
):
    page, did = form
    _submit(owner_wall, page, {"region": "a"})
    agent_read = decisions.get_owned(db, "ops", did).answer_record
    stored = _rows(db, "select answer_record from form_answer where person = 'jeremy'")
    assert agent_read == stored[0][0]


def test_only_the_person_a_form_is_addressed_to_may_submit_it(
    db, form, owner_wall, ann_wall
):
    page, did = form
    # unaddressed: the owner's
    assert _submit(ann_wall, page, {"region": "a"}).status_code == 404
    assert _rows(db, "select * from form_answer") == []
    assert _submit(owner_wall, page, {"region": "a"}).status_code == 200
    # addressed to Ann: hers, and no longer the owner's
    _address(db, did, "ann")
    assert _submit(owner_wall, page, {"region": "b"}).status_code == 404
    assert _submit(ann_wall, page, {"region": "b"}).status_code == 200
    assert sorted(r[0] for r in _rows(db, "select person from form_answer")) == [
        "ann",
        "jeremy",
    ]


def test_the_gallery_draws_the_form_with_the_readers_own_answers(
    db, form, owner_wall, ann_wall
):
    page, did = form
    _address(db, did, None)
    _submit(owner_wall, page, {"region": "a"})
    formanswer.submit(db, did, version=1, values={"region": "b"}, by="ann")
    mine = owner_wall.get(f"/a/{page.id}/forms.json").json()["forms"][0]
    hers = ann_wall.get(f"/a/{page.id}/forms.json").json()["forms"][0]
    assert mine["carried"]["region"] == "a"
    assert hers["carried"]["region"] == "b"
    assert hers["answered"]["answered_by"] == "ann"


def test_a_republish_carries_each_persons_record_onto_the_new_version(
    db, form, owner_wall
):
    page, did = form
    _submit(owner_wall, page, {"region": "a", "why": "keep"})
    formanswer.submit(db, did, version=1, values={"region": "b"}, by="ann")
    store.patch(
        db,
        "ops",
        page.id,
        "<!doctype html><html><head><title>P</title></head><body>"
        + FORM.replace("Why <", "Because <")
        + "</body></html>",
        page.revision,
    )
    got = {
        r[0]: r[1] for r in _rows(db, "select person, answer_record from form_answer")
    }
    assert got["jeremy"]["version"] == 2 and got["ann"]["version"] == 2
    assert got["jeremy"]["answers"]["region"] == "a"
    assert got["ann"]["answers"]["region"] == "b"


def test_a_plain_question_is_answered_only_by_the_person_it_is_for(
    db, owner_wall, ann_wall
):
    page = store.publish(db, "ops", "T", "<p>x</p>")
    row = decisions.ask(
        db, agent="ops", artifact_id=page.id, question="q?", options=OPTIONS
    )
    answer = lambda wall: wall.post(
        f"/d/{row.id}/answer",
        data={"letter": "a"},
        headers=SAME,
        follow_redirects=False,
    )
    assert answer(ann_wall).status_code == 404
    assert decisions.get_owned(db, "ops", row.id).answer is None
    _address(db, row.id, "ann")
    assert answer(owner_wall).status_code == 404
    assert answer(ann_wall).status_code in (200, 303)
    assert decisions.get_owned(db, "ops", row.id).answered_by == "ann"


def test_a_receipt_is_about_the_answer_of_the_person_who_gave_it(db, owner_wall):
    page = store.publish(db, "ops", "T", "<p>x</p>")
    row = decisions.ask(
        db, agent="ops", artifact_id=page.id, question="q?", options=OPTIONS
    )
    owner_wall.post(
        f"/d/{row.id}/answer",
        data={"letter": "a"},
        headers=SAME,
        follow_redirects=False,
    )
    receipts.declare_on(db, None, "ops", row.id, state="working", note="on it")
    assert _rows(db, "select person from receipt") == [("jeremy",)]
    # the agent's own read of the receipt is as it was: no person in it
    with db.connect() as conn:
        assert all("person" not in r for r in receipts.history(conn, row.id))


def test_another_persons_answer_shows_the_owner_no_receipt_and_no_pickup(
    db, owner_wall, ann_wall
):
    page = store.publish(db, "ops", "T", "<p>x</p>")
    row = decisions.ask(
        db, agent="ops", artifact_id=page.id, question="q?", options=OPTIONS
    )
    _address(db, row.id, "ann")
    answered = ann_wall.post(
        f"/d/{row.id}/answer",
        data={"letter": "a"},
        headers=SAME,
        follow_redirects=False,
    )
    assert answered.status_code in (200, 303)
    # Ann sees her own answer as not picked up; the owner sees nothing said of it
    assert "not picked up yet" in ann_wall.get(f"/a/{page.id}").text
    receipts.declare_on(db, None, "ops", row.id, state="working", note="on it")
    seen_by_ann = ann_wall.get(f"/a/{page.id}").text
    assert 'class="pickup rstrip' in seen_by_ann
    seen_by_owner = owner_wall.get(f"/a/{page.id}").text
    assert "not picked up yet" not in seen_by_owner
    assert 'class="pickup rstrip' not in seen_by_owner
    assert "picked this up" not in seen_by_owner


def _older_image_writes(db, did, person, **changes):
    """What an image from before slice 3 does after a migration: it writes only
    `decision.answer_record` (a new submit, its history entry), never `form_answer`."""
    with db.connect() as conn:
        record = conn.execute(
            "select answer_record from decision where id = %s", (did,)
        ).fetchone()[0]
        record = {
            **record,
            "answers": {**record["answers"], **changes},
            "submit_id": "older-image-submit",
            "history": [
                *record["history"],
                {"submit_id": "older-image-submit", "answered_by": person},
            ],
        }
        conn.execute(
            "update decision set answer_record = %s where id = %s",
            (jsonb(record), did),
        )
    return record


@pytest.mark.parametrize("row_is", ["stale", "missing"])
@pytest.mark.parametrize("write", ["submit", "relay"])
def test_an_answer_an_older_image_wrote_is_picked_up_by_the_next_write(
    db, form, owner_wall, row_is, write
):
    page, did = form
    assert _submit(owner_wall, page, {"region": "a", "why": "mine"}).status_code == 200
    older = _older_image_writes(db, did, "jeremy", why="older")
    if row_is == "missing":
        with db.connect() as conn:
            conn.execute("delete from form_answer where decision_id = %s", (did,))
    # the form bar draws what the older image wrote, not the stale (or missing) row
    shown = owner_wall.get(f"/a/{page.id}/forms.json").json()["forms"][0]
    assert shown["carried"]["why"] == "older"
    if write == "submit":
        formanswer.submit(db, did, version=1, values={"region": "b"}, by="jeremy")
    else:
        formanswer.relay(
            db,
            did,
            fields_given={"why": "tg"},
            note="n",
            source="telegram",
            recorded_by="ops",
        )
    (mine,) = _rows(
        db, "select answer_record from form_answer where decision_id = %s", did
    )
    mine = mine[0]
    kept = {h["submit_id"] for h in mine["history"]}
    assert "older-image-submit" in kept and len(mine["history"]) == 3
    if write == "submit":
        assert mine["answers"]["why"] == "older" and mine["answers"]["region"] == "b"
    else:
        assert mine["answers"]["region"] == "a"
    # the repaired row is what the agent reads too
    assert decisions.get_owned(db, "ops", did).answer_record == mine
    assert older["history"][:2] == mine["history"][:2]


@pytest.mark.parametrize("row_is", ["stale", "missing"])
def test_a_relay_that_changes_nothing_still_repairs_the_row_an_older_image_left(
    db, form, owner_wall, row_is
):
    page, did = form
    assert _submit(owner_wall, page, {"region": "a", "why": "mine"}).status_code == 200
    _older_image_writes(db, did, "jeremy", why="older")
    if row_is == "missing":
        with db.connect() as conn:
            conn.execute("delete from form_answer where decision_id = %s", (did,))
    done = formanswer.relay(
        db,
        did,
        fields_given={"region": "b"},
        note="n",
        source="telegram",
        recorded_by="ops",
        said_at=datetime.now(UTC) - timedelta(days=1),
    )
    assert done["applied"] == [] and done["skipped"] == ["region"]
    (mine,) = _rows(
        db, "select answer_record from form_answer where decision_id = %s", did
    )
    assert mine[0]["answers"]["why"] == "older"
    assert "older-image-submit" in {h["submit_id"] for h in mine[0]["history"]}
    assert decisions.get_owned(db, "ops", did).answer_record == mine[0]


def test_an_older_images_republish_leaves_no_old_answer_in_the_bar(
    db, form, owner_wall
):
    """An older image reconciles only `decision.answer_record` at a republish: the
    changed field is blanked and goes stale there, while the person's `form_answer` row
    stays on version 1 with the same submit_id. After the roll forward the bar must not
    carry the old answer into the new question."""
    page, did = form
    assert _submit(owner_wall, page, {"region": "a", "why": "keep"}).status_code == 200
    (before,) = _rows(
        db, "select answer_record from form_answer where decision_id = %s", did
    )
    store.patch(
        db,
        "ops",
        page.id,
        "<!doctype html><html><head><title>P</title></head><body>"
        + FORM.replace("Why <", "Because <")
        + "</body></html>",
        page.revision,
    )
    with db.connect() as conn:  # the older image never touched the row
        conn.execute(
            "update form_answer set answer_record = %s where decision_id = %s",
            (jsonb(before[0]), did),
        )
        decided = conn.execute(
            "select answer_record from decision where id = %s", (did,)
        ).fetchone()[0]
    assert before[0]["version"] == 1 and decided["version"] == 2
    assert decided["submit_id"] == before[0]["submit_id"]
    shown = owner_wall.get(f"/a/{page.id}/forms.json").json()["forms"][0]
    assert shown["version"] == 2
    assert "why" not in shown["carried"]
    assert shown["carried"]["region"] == "a"
    assert [s["name"] for s in shown["stale"]] == ["why"]
    assert shown["stale"][0]["value"] == "keep"
    # reading it stored nothing: the row is still the older image's
    (after,) = _rows(
        db, "select answer_record from form_answer where decision_id = %s", did
    )
    assert after[0]["version"] == 1


@pytest.mark.parametrize("row_is", ["stale", "missing"])
def test_an_unapplied_relay_keeps_the_decisions_answered_at_on_the_row(
    db, form, owner_wall, row_is
):
    page, did = form
    assert _submit(owner_wall, page, {"region": "a", "why": "mine"}).status_code == 200
    _older_image_writes(db, did, "jeremy", why="older")
    with db.connect() as conn:
        long_ago = datetime.now(UTC) - timedelta(days=2)
        conn.execute(
            "update decision set answered_at = %s where id = %s", (long_ago, did)
        )
        if row_is == "missing":
            conn.execute("delete from form_answer where decision_id = %s", (did,))
    done = formanswer.relay(
        db,
        did,
        fields_given={"region": "b"},
        note="n",
        source="telegram",
        recorded_by="ops",
        said_at=datetime.now(UTC) - timedelta(days=3),
    )
    assert done["applied"] == []
    (row,) = _rows(
        db,
        "select f.answered_at, d.answered_at from form_answer f"
        " join decision d on d.id = f.decision_id where f.decision_id = %s",
        did,
    )
    assert row[0] == row[1]
