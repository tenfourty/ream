"""Plug-in API 1.5 (core-and-plugins design, step 9e-4): one plug-in may register the
'slack' answer source, never as a relay; an inbound answer names who vouched for it;
and a press in Slack never replaces his own tap on the page."""

import itertools
import sys
import types

import pytest

from ream import decisions, plugin_api, sinks, store

pytestmark = pytest.mark.plugin_loader

_PAGE = "<!doctype html><html><head><title>t</title></head><body>hi</body></html>"
_counter = itertools.count()


class EP:
    """An entry point: a name and the `register` it loads."""

    def __init__(self, name, register):
        self.name = name
        self._register = register

    def load(self):
        return self._register


def _plugin(name, register):
    """An entry point whose `register` has a module of its own declaring API 1."""
    mod = types.ModuleType(f"fake_plugin_1_5_mod_{next(_counter)}")
    mod.REQUIRES_API = "1"
    register.__module__ = mod.__name__
    sys.modules[mod.__name__] = mod
    return EP(name, register)


@pytest.fixture()
def world(monkeypatch):
    """The process-global registries restored after each test; entry points set by
    `install`."""
    monkeypatch.setattr(decisions, "_REGISTERED", list(decisions._REGISTERED))
    monkeypatch.setattr(decisions, "_LABELS", dict(decisions._LABELS))
    monkeypatch.setattr(decisions, "_RELAY", list(decisions._RELAY))
    monkeypatch.setattr(decisions, "_VERIFIED_LABELS", dict(decisions._VERIFIED_LABELS))
    monkeypatch.setattr(decisions, "_ROUTE_LABELS", dict(decisions._ROUTE_LABELS))
    monkeypatch.setattr(decisions, "_ANSWER_HOOKS", [])
    monkeypatch.setattr(plugin_api, "_SINGLE_SOURCE_HOLDERS", {})
    monkeypatch.setattr(plugin_api, "_failures", {})
    monkeypatch.setattr(plugin_api, "_loaded", [])

    def install(*eps):
        monkeypatch.setattr(plugin_api, "_entry_points", lambda: list(eps))

    with sinks.isolated():
        yield install


def _ask(db):
    page = store.publish(db, "seat-a", "Plan", _PAGE)
    return decisions.ask(
        db,
        agent="seat-a",
        artifact_id=page.id,
        question="go?",
        options=[{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}],
    )


def _stored(db, decision_id):
    with db.connect() as conn:
        return conn.execute(
            "select answer, answer_note, answer_source, recorded_by from decision"
            " where id = %s",
            (decision_id,),
        ).fetchone()


def _answered_events(db):
    with db.connect() as conn:
        (n,) = conn.execute(
            "select count(*) from event where kind = 'decision_answered'"
        ).fetchone()
    return n


def test_api_is_1_5():
    assert plugin_api.API_VERSION == (1, 5)


def test_a_plugin_may_register_slack_and_reword_it(world):
    def register(api):
        api.answer_source("slack", "Slack (qd)", verified_label="quarterdeck")

    world(_plugin("qd", register))
    assert plugin_api.load() == ["qd"]
    assert decisions.source_label("slack") == "Slack (qd)"
    assert decisions.verified_label_of("slack") == "quarterdeck"
    assert "slack" not in decisions.relay_sources()


def test_slack_cannot_be_registered_as_a_relay(world):
    def register(api):
        api.answer_source("slack", "Mine", relay=True)

    world(_plugin("toy", register))
    with pytest.raises(plugin_api.PluginError, match="toy"):
        plugin_api.load()
    assert "slack" not in decisions.relay_sources()
    # Refused before anything was changed: core's own label still reads.
    assert decisions.source_label("slack") == "Slack"
    # The refusal held nothing: another plug-in may still take it.
    assert plugin_api._SINGLE_SOURCE_HOLDERS == {}


def test_only_one_plugin_may_register_slack(world):
    def one(api):
        api.answer_source("slack", "Slack")
        api.answer_source("slack", "Slack")  # the same plug-in again: fine

    def two(api):
        api.answer_source("slack", "Second")

    world(_plugin("one", one), _plugin("two", two))
    with pytest.raises(plugin_api.PluginError, match="two") as err:
        plugin_api.load()
    assert "'one'" in str(err.value)
    assert decisions.source_label("slack") == "Slack"  # the second changed nothing


def test_an_inbound_answer_needs_recorded_by(db, world):
    def register(api):
        api.answer_source("toy-chat", "Toy chat")

    world(_plugin("toy", register))
    plugin_api.load()
    q = _ask(db)
    for source in ("slack", "toy-chat"):
        for nobody in (None, ""):
            with pytest.raises(decisions.InvalidSource):
                decisions.answer(
                    db, q.id, letter="a", by="owner", source=source, recorded_by=nobody
                )
    assert _stored(db, q.id) == (None, "", None, None)
    assert _answered_events(db) == 0


def test_a_page_answer_takes_no_recorded_by(db, world):
    world()
    q = _ask(db)
    with pytest.raises(decisions.InvalidSource):
        decisions.answer(db, q.id, letter="a", by="owner", recorded_by="seat-a")
    assert _stored(db, q.id) == (None, "", None, None)
    decisions.answer(db, q.id, letter="a", by="owner")
    assert _stored(db, q.id) == ("a", "", "page", None)


def test_a_slack_press_leaves_his_page_tap_standing(db, world):
    world()
    q = _ask(db)
    decisions.answer(db, q.id, letter="a", note="mine", by="owner")
    before = _answered_events(db)
    ran = []
    with pytest.raises(decisions.TapStands) as err:
        decisions.answer(
            db,
            q.id,
            letter="b",
            note="pressed",
            by="owner",
            source="slack",
            recorded_by="quarterdeck",
            in_transaction=ran.append,
        )
    assert (err.value.row.answer, err.value.row.answer_source) == ("a", "page")
    assert _stored(db, q.id) == ("a", "mine", "page", None)
    assert _answered_events(db) == before and ran == []
    # He may still change his mind on the page.
    decisions.answer(db, q.id, letter="b", by="owner")
    assert _stored(db, q.id) == ("b", "", "page", None)


def test_a_slack_press_still_replaces_an_earlier_slack_press(db, world):
    world()
    q = _ask(db)
    for letter in ("a", "b"):
        decisions.answer(
            db,
            q.id,
            letter=letter,
            by="owner",
            source="slack",
            recorded_by="quarterdeck",
        )
    assert _stored(db, q.id) == ("b", "", "slack", "quarterdeck")


def test_core_alone_still_stores_a_slack_answer(db, world):
    world()
    assert plugin_api.load() == []
    assert plugin_api._SINGLE_SOURCE_HOLDERS == {}
    q = _ask(db)
    row = decisions.answer(
        db, q.id, letter="a", by="owner", source="slack", recorded_by="quarterdeck"
    )
    assert (row.answer, row.answer_source, row.recorded_by) == (
        "a",
        "slack",
        "quarterdeck",
    )
    assert _stored(db, q.id) == ("a", "", "slack", "quarterdeck")
    assert decisions.is_known_source("slack")
    assert decisions.source_label("slack") == "Slack"
    assert "slack" not in decisions.relay_sources()
