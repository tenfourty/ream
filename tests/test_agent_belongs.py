"""An agent belongs to someone, and a question with no `addressed_to` goes to the owners of the
page's agent (agents design, slice 1, docs/agent/specs/2026-11-26-agents-and-watching-design.md).

A second person is the app agent `mcp_agent` names after them (a personal agent); every key agent
is shared. `people.OPEN_SIGN_IN` is flipped as `test_per_person_state.py` does. With one person
(only shared agents) nothing changes: the owner answers, as before."""

import pytest

from ream import (
    agents,
    decisions,
    forms_ui,
    mcp_apps,
    people,
    settings,
    store,
)

OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]
ANN = people.Person(id="ann", name="Ann", is_owner=False)
CLIENT = "https://claude.ai/oauth/claude-ai/client.json"
FORM = (
    "<!doctype html><html><head><title>P</title></head><body>"
    '<form data-artifact-form="rollout"><label>Why <input type="text" name="why"></label>'
    "</form></body></html>"
)


@pytest.fixture(autouse=True)
def _sign_in_open(monkeypatch):
    monkeypatch.setattr(people, "OPEN_SIGN_IN", True)


@pytest.fixture()
def ann_agent(db):
    return mcp_apps.agent_for(db, "ann", CLIENT, "claude.ai").agent


def _ask(db, agent):
    page = store.publish(db, agent, "Q", "<p>x</p>")
    row = decisions.ask(
        db, agent=agent, artifact_id=page.id, question="which?", options=OPTIONS
    )
    return page, row


def _answer(db, row, who):
    person = people.owner_person() if who == "jeremy" else ANN
    return decisions.answer(db, row.id, letter="a", by=person.id, person=person)


def test_an_app_agent_belongs_to_its_person_and_a_key_agent_is_shared(db, ann_agent):
    with db.connect() as conn:
        assert agents.person_of(conn, ann_agent) == "ann"
        assert agents.page_owners(conn, ann_agent) == ("ann",)
        assert agents.person_of(conn, "ops") is None
        assert agents.page_owners(conn, "ops") == (agents.shared_owner(),)
        assert agents.shared_owner() == settings.owner_id()
    with settings.override(owner_id="boss"), db.connect() as conn:
        assert agents.page_owners(conn, "ops") == ("boss",)  # read at call time


def test_a_personal_agents_question_is_answered_only_by_its_person(db, ann_agent):
    _, row = _ask(db, ann_agent)
    assert row.addressed_to is None  # routing is derived: nothing is written
    with pytest.raises(store.NotFound):
        _answer(db, row, "jeremy")
    assert _answer(db, row, "ann").answer == "a"


def test_a_shared_agents_question_is_answered_only_by_the_owner(db):
    _, row = _ask(db, "ops")
    with pytest.raises(store.NotFound):
        _answer(db, row, "ann")
    assert _answer(db, row, "jeremy").answer == "a"


def test_refusing_is_the_404_a_missing_decision_gets(db, ann_agent):
    _, row = _ask(db, ann_agent)
    with pytest.raises(store.NotFound) as refused:
        _answer(db, row, "jeremy")
    with pytest.raises(store.NotFound) as missing:
        decisions.answer(
            db,
            "no-such",
            letter="a",
            by="jeremy",
            person=people.owner_person(),
        )
    assert type(refused.value) is type(missing.value)
    assert str(refused.value) == str(missing.value)


def test_an_explicit_addressed_to_still_wins_on_either_kind_of_agent(db, ann_agent):
    for agent in ("ops", ann_agent):
        _, row = _ask(db, agent)
        with db.connect() as conn:
            conn.execute(
                "update decision set addressed_to = 'ann' where id = %s", (row.id,)
            )
        with pytest.raises(store.NotFound):
            _answer(db, row, "jeremy")
        assert _answer(db, row, "ann").answered_by == "ann"


def test_an_apps_relay_cannot_record_for_a_personal_agents_question(db, ann_agent):
    # A signed-in app is the owner's until slice 2: it may not pass on someone else's answer.
    _, theirs = _ask(db, ann_agent)
    with pytest.raises(store.NotFound):
        decisions.record_elsewhere(
            db,
            theirs.id,
            letter="a",
            note="n",
            source="mcp",
            recorded_by="x",
            by_app=True,
        )
    _, shared = _ask(db, "ops")
    done = decisions.record_elsewhere(
        db, shared.id, letter="a", note="n", source="mcp", recorded_by="x", by_app=True
    )
    assert done.answered_by == "jeremy"


def test_a_relay_of_a_personal_agents_question_files_under_its_person(db, ann_agent):
    _, row = _ask(db, ann_agent)
    done = decisions.record_elsewhere(
        db, row.id, letter="a", note="n", source="telegram", recorded_by="ops"
    )
    assert done.answered_by == "ann"


def test_the_lists_and_counts_follow_the_same_rule_in_sql(db, ann_agent):
    _ask(db, ann_agent)
    _ask(db, "ops")

    def mine(person):
        rows = decisions.across_seats(db, person=person)
        return sorted(r.agent for r in rows)

    assert mine("jeremy") == ["ops"]
    assert mine("ann") == [ann_agent]
    assert mine(None) == sorted(["ops", ann_agent])  # the agent feeds pass no person
    assert decisions.open_counts(db, "jeremy")[2] == 1
    assert decisions.open_counts(db, "ann")[2] == 1
    assert decisions.open_counts(db)[2] == 2
    # the stored agent field is the agent's name, never a display line
    assert all(
        r.agent in ("ops", ann_agent) for r in decisions.across_seats(db, person=None)
    )


def test_the_form_bar_is_drawn_only_for_the_person_who_may_answer(db, ann_agent):
    page = store.publish(db, ann_agent, "Board", FORM)
    assert forms_ui.payload(db, page.id, "ann")["forms"]
    assert not forms_ui.payload(db, page.id, "jeremy")["forms"]
    shared = store.publish(db, "ops", "Board", FORM)
    assert forms_ui.payload(db, shared.id, "jeremy")["forms"]
    assert not forms_ui.payload(db, shared.id, "ann")["forms"]
