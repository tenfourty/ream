"""A deleted page's questions and comments leave the agent's lists (ij8.110).

Found 28-29 Sep 2026: an answered question on a deleted page was listed, but
recording its outcome answered 404 -- the list and the route disagreed about
whether it existed. A comment thread on a deleted page kept listing as NEEDS
YOUR REPLY and woke the seat's waiter again and again. The rule is the one the
uncollected alert already follows (ij8.97): a deleted page is gone from the
agent's view, and restoring it brings everything back.
"""

from ream import comments, decisions, store

PAGE = "<!doctype html><html><head><title>P</title></head><body><p>x</p></body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


def _page_with_answer_and_comment(db):
    page = store.publish(db, "seat", "P", PAGE)
    asked = decisions.ask(
        db, agent="seat", artifact_id=page.id, question="q?", options=OPTIONS
    )
    decisions.answer(db, asked.id, letter="a", by="jeremy")
    comments.add(
        db, artifact_id=page.id, author_kind="human", author="jeremy", body="hi"
    )
    return page, asked


def _listed(db):
    qs = decisions.list_for_agent(db, "seat", include_answered=True)
    cs = comments.list_for_agent(db, "seat")
    return [q.id for q in qs], [c.body for c in cs]


def test_a_deleted_pages_question_and_comment_leave_the_agents_lists(db):
    page, asked = _page_with_answer_and_comment(db)
    qs, cs = _listed(db)
    assert asked.id in qs and "hi" in cs
    store.set_deleted(db, page.id, True, agent="seat")
    qs, cs = _listed(db)
    assert asked.id not in qs
    assert "hi" not in cs


def test_restoring_the_page_brings_them_back(db):
    page, asked = _page_with_answer_and_comment(db)
    store.set_deleted(db, page.id, True, agent="seat")
    store.set_deleted(db, page.id, False, agent="seat")
    qs, cs = _listed(db)
    assert asked.id in qs and "hi" in cs


def test_an_archived_pages_questions_stay_listed(db):
    """Archiving is 'not now', not 'gone': its conversations stay in view."""
    page, asked = _page_with_answer_and_comment(db)
    store.set_archived(db, page.id, True, agent="seat")
    qs, cs = _listed(db)
    assert asked.id in qs and "hi" in cs
