"""Review requests and verdicts (page review step 2, homelab-ij8.108)."""

import pytest

from artifacts import decisions, events, store

PAGE = (
    "<!doctype html><title>Plan</title>"
    "<p>Rotate the keys every 90 days.</p><p>Email the team when done.</p>"
)

TWO = [{"letter": "a", "label": "x"}, {"letter": "b", "label": "y"}]


def _page(db, agent="seat"):
    return store.publish(db, agent, "Plan", PAGE)


def test_a_review_is_a_decision_with_four_fixed_verdicts(db):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    assert row.kind == "review"
    assert [o["label"] for o in row.options] == [
        "Approved",
        "Approved with notes",
        "Changes requested",
        "Dismissed",
    ]
    assert row.recommended is None
    assert row.question.startswith("Review")


def test_an_ordinary_question_is_still_kind_question(db):
    page = _page(db)
    row = decisions.ask(
        db, agent="seat", artifact_id=page.id, question="q?", options=TWO
    )
    assert row.kind == "question"


def test_only_the_owner_can_ask_for_a_review(db):
    page = _page(db)
    with pytest.raises(store.NotFound):
        decisions.ask_review(db, agent="someone-else", artifact_id=page.id)


def test_the_first_verdict_counts_and_a_second_is_refused(db):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    decisions.answer(db, row.id, letter="c", by="jeremy")
    with pytest.raises(decisions.AlreadyDecided) as err:
        decisions.answer(db, row.id, letter="a", by="jeremy")
    assert err.value.row.answer == "c"
    assert err.value.field == "answer"
    [stored] = decisions.list_for_artifact(db, page.id, include_answered=True)
    assert stored.answer == "c", "the counted verdict is unchanged"


def test_a_question_can_still_be_re_answered(db):
    page = _page(db)
    row = decisions.ask(
        db, agent="seat", artifact_id=page.id, question="q?", options=TWO
    )
    decisions.answer(db, row.id, letter="a", by="jeremy")
    assert decisions.answer(db, row.id, letter="b", by="jeremy").answer == "b"


def test_a_verdict_wakes_the_agent_like_an_answer(db):
    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    decisions.answer(db, row.id, letter="b", note="small things", by="jeremy")
    kinds = [e.kind for e in events.list_for_agent(db, "seat")]
    assert "decision_answered" in kinds


def test_the_asking_session_still_rides_the_answer_event(db):
    page = _page(db)
    row = decisions.ask_review(
        db, agent="seat", artifact_id=page.id, session="seat/one"
    )
    decisions.answer(db, row.id, letter="a", by="jeremy")
    [ev] = [
        e for e in events.list_for_agent(db, "seat") if e.kind == "decision_answered"
    ]
    assert ev.session == "seat/one"
