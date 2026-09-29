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


def test_two_simultaneous_verdicts_keep_only_the_first(db, monkeypatch):
    """Both callers pass the read-side check before either writes: the barrier
    sits in `_check_secrets`, which runs after that check and before the write."""
    import threading

    page = _page(db)
    row = decisions.ask_review(db, agent="seat", artifact_id=page.id)
    barrier = threading.Barrier(2, timeout=10)
    real = store._check_secrets

    def gated(*args, **kwargs):
        real(*args, **kwargs)
        barrier.wait()

    monkeypatch.setattr(store, "_check_secrets", gated)
    outcomes = {}

    def give(letter):
        try:
            outcomes[letter] = decisions.answer(db, row.id, letter=letter, by="jeremy")
        except decisions.AlreadyDecided as err:
            outcomes[letter] = err

    threads = [threading.Thread(target=give, args=(x,)) for x in ("a", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    winners = [k for k, v in outcomes.items() if isinstance(v, decisions.DecisionRow)]
    losers = [k for k, v in outcomes.items() if isinstance(v, decisions.AlreadyDecided)]
    assert len(winners) == 1 and len(losers) == 1
    [stored] = decisions.list_for_artifact(db, page.id, include_answered=True)
    assert stored.answer == winners[0]
    assert outcomes[losers[0]].row.answer == winners[0]
    answered = [
        e for e in events.list_for_agent(db, "seat") if e.kind == "decision_answered"
    ]
    assert len(answered) == 1
