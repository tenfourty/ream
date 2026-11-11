"""The plug-in API and loader (core-and-plugins design, slice 7a): a throwaway
in-tree plug-in for the sink and the answer source, and the failure cases.
Plug-ins are loaded without installing a package: `_entry_points` is replaced."""

import itertools
import sys
import types

import pytest

from ream import decisions, events, plugin_api, settings, sinks, store

pytestmark = pytest.mark.plugin_loader


class EP:
    """An entry point: a name and the `register` it loads."""

    def __init__(self, name, register):
        self.name = name
        self._register = register

    def load(self):
        return self._register


_counter = itertools.count()


def _with_module(fn, requires, monkeypatch=None):
    """Give `fn` a module of its own declaring `REQUIRES_API` (or not)."""
    mod = types.ModuleType(f"fake_plugin_mod_{next(_counter)}")
    if requires is not None:
        mod.REQUIRES_API = requires
    fn.__module__ = mod.__name__
    sys.modules[mod.__name__] = mod
    return fn


@pytest.fixture()
def world(monkeypatch):
    """Registries restored after each test; entry points set by `install`."""
    monkeypatch.setattr(decisions, "_REGISTERED", list(decisions._REGISTERED))
    monkeypatch.setattr(decisions, "_LABELS", dict(decisions._LABELS))
    monkeypatch.setattr(decisions, "_RELAY", list(decisions._RELAY))
    monkeypatch.setattr(decisions, "_VERIFIED_LABELS", dict(decisions._VERIFIED_LABELS))
    monkeypatch.setattr(decisions, "_ROUTE_LABELS", dict(decisions._ROUTE_LABELS))
    monkeypatch.setattr(decisions, "_ANSWER_HOOKS", [])
    monkeypatch.setattr(plugin_api, "_failures", {})
    monkeypatch.setattr(plugin_api, "_loaded", [])

    def install(*eps):
        monkeypatch.setattr(plugin_api, "_entry_points", lambda: list(eps))

    with sinks.isolated():
        yield install


def _event():
    return events.Event(
        kind="decision_answered",
        artifact_id="art1",
        agent="seat-a",
        actor="owner",
        summary="s",
    )


def _plugin(seen):
    def register(api):
        assert api.version == plugin_api.API_VERSION == (1, 2)
        api.sink("toy", ("decision_answered",), seen.append, "after_commit")
        api.answer_source(
            "toy-chat",
            "Toy",
            relay=True,
            verified_label="Toy verified",
            route_label="Toy route",
        )

    return _with_module(register, "1")


def test_a_plugin_registers_a_sink_and_an_answer_source(world):
    seen = []
    world(EP("toy", _plugin(seen)))
    assert plugin_api.load() == ["toy"]
    assert plugin_api.loaded() == ["toy"]
    assert [s.name for s in sinks.matching("decision_answered")] == ["toy"]
    assert decisions.is_known_source("toy-chat")
    assert decisions.source_label("toy-chat") == "Toy"
    assert decisions.verified_label_of("toy-chat") == "Toy verified"
    assert decisions.route_label_of("toy-chat") == "Toy route"
    assert "toy-chat" in decisions.relay_sources()
    sinks.deliver_late(sinks.matching("decision_answered"), _event())
    assert len(seen) == 1


def test_a_source_without_relay_is_not_claimable_by_an_agent(world):
    def register(api):
        api.answer_source("quiet", "Quiet")

    world(EP("q", _with_module(register, None)))
    plugin_api.load()
    assert decisions.is_known_source("quiet")
    assert "quiet" not in decisions.relay_sources()
    assert decisions.relay_sources() == decisions.ANSWER_SOURCES
    assert decisions.verified_label_of("quiet") == "Quiet"
    assert decisions.route_label_of("quiet") == "Quiet"


def test_loading_goes_in_name_order(world):
    order = []

    def make(tag):
        def register(api):
            order.append(tag)

        return _with_module(register, None)

    world(EP("b", make("b")), EP("a", make("a")), EP("c", make("c")))
    assert plugin_api.load() == ["a", "b", "c"]
    assert order == ["a", "b", "c"]


def test_a_register_that_raises_stops_start_up_naming_it(world):
    def register(api):
        raise RuntimeError("no")

    world(EP("broken", _with_module(register, "1")))
    with pytest.raises(plugin_api.PluginError, match="broken"):
        plugin_api.load()


def test_a_wrong_major_version_stops_start_up_naming_it(world):
    called = []

    def register(api):
        called.append(1)

    world(EP("future", _with_module(register, "2")))
    with pytest.raises(plugin_api.PluginError, match="future"):
        plugin_api.load()
    assert called == []


def test_an_unknown_name_in_the_list_stops_start_up_naming_it(world):
    world(EP("toy", _plugin([])))
    with (
        settings.override(plugins="toy,ghost"),
        pytest.raises(plugin_api.PluginError, match="ghost"),
    ):
        plugin_api.load()
    assert [s.name for s in sinks.registered()] == []


def test_none_loads_nothing_and_a_list_loads_only_those(world):
    world(EP("toy", _plugin([])), EP("other", _with_module(lambda api: None, None)))
    with settings.override(plugins="none"):
        assert plugin_api.load() == []
    assert not decisions.is_known_source("toy-chat")
    with settings.override(plugins="other"):
        assert plugin_api.load() == ["other"]
    assert not decisions.is_known_source("toy-chat")


def test_a_sink_that_raises_is_isolated_and_counted(world, caplog):
    def deliver(event):
        raise RuntimeError("down")

    def register(api):
        api.sink("flaky", ("decision_answered",), deliver, "after_commit")

    world(EP("flaky-plugin", _with_module(register, "1")))
    plugin_api.load()
    before = sinks.late_failures()
    with caplog.at_level("WARNING"):
        sinks.deliver_late(sinks.matching("decision_answered"), _event())  # no raise
    assert plugin_api.failure_counts() == {"flaky-plugin:sink": 1}
    assert sinks.late_failures() == before + 1
    assert "flaky-plugin" in caplog.text and "sink" in caplog.text


def test_an_outbox_sink_that_raises_is_counted_and_still_retried(world):
    def deliver(event):
        raise RuntimeError("down")

    def register(api):
        api.sink("flaky", ("decision_answered",), deliver)

    world(EP("flaky-plugin", _with_module(register, "1")))
    plugin_api.load()
    (sink,) = sinks.matching("decision_answered")
    with pytest.raises(RuntimeError):  # the sender sees it and backs off
        sink.deliver(_event())
    assert plugin_api.failure_counts() == {"flaky-plugin:sink": 1}


def test_with_no_plugins_core_keeps_its_own_sinks_and_slack(world):
    world()
    assert plugin_api.load() == []
    assert decisions.is_known_source("slack")


# --- on_answer (slice 7b part 1) ---------------------------------------------------

_PAGE = "<!doctype html><html><head><title>t</title></head><body>hi</body></html>"


def _ask(db):
    page = store.publish(db, "seat-a", "Plan", _PAGE)
    return decisions.ask(
        db,
        agent="seat-a",
        artifact_id=page.id,
        question="go?",
        options=[{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}],
    )


def test_on_answer_runs_inside_the_transaction_for_the_plugins_own_source(db, world):
    seen = []

    def hook(conn, decision, answer):
        # Inside the transaction: the row is already written, visible on this conn.
        row = conn.execute(
            "select answer from decision where id = %s", (decision.id,)
        ).fetchone()
        seen.append((row[0], decision.answer, answer))

    def register(api):
        api.answer_source("toy-chat", "Toy chat")
        api.on_answer("mark", hook)

    world(EP("toy", _with_module(register, "1")))
    plugin_api.load()
    decisions.answer(
        db,
        _ask(db).id,
        letter="a",
        note="n",
        by="owner",
        source="toy-chat",
        recorded_by="toy",
    )
    assert len(seen) == 1
    stored, on_row, answer = seen[0]
    assert stored == on_row == "a"
    assert (answer.letter, answer.note, answer.source, answer.recorded_by) == (
        "a",
        "n",
        "toy-chat",
        "toy",
    )


def test_on_answer_does_not_run_for_a_source_the_plugin_did_not_register(db, world):
    seen = []

    def register(api):
        api.answer_source("toy-chat", "Toy chat")
        api.on_answer("mark", lambda conn, d, a: seen.append(a.source))

    world(EP("toy", _with_module(register, "1")))
    plugin_api.load()
    decisions.answer(db, _ask(db).id, letter="a", by="owner")  # a tap
    assert seen == []


def test_a_raising_hook_rolls_the_answer_back_naming_the_plugin(db, world, caplog):
    def boom(conn, decision, answer):
        raise RuntimeError("secret detail")

    def register(api):
        api.answer_source("toy-chat", "Toy chat")
        api.on_answer("mark", boom)

    world(EP("toy", _with_module(register, "1")))
    plugin_api.load()
    q = _ask(db)
    with caplog.at_level("WARNING"), pytest.raises(decisions.AnswerHookFailed) as err:
        decisions.answer(db, q.id, letter="a", by="owner", source="toy-chat")
    assert "toy" in str(err.value) and "secret" not in str(err.value)
    assert "secret" not in caplog.text
    with db.connect() as conn:
        (answer, answered_at, acked_at) = conn.execute(
            "select answer, answered_at, acknowledged_at from decision where id = %s",
            (q.id,),
        ).fetchone()
        (n,) = conn.execute(
            "select count(*) from event where kind = 'decision_answered'"
        ).fetchone()
        (outbox,) = conn.execute("select count(*) from outbox").fetchone()
        (partner,) = conn.execute("select count(*) from partner_outbox").fetchone()
    assert answer is None and n == 0  # not half-applied
    assert answered_at is None and acked_at is None
    assert outbox == 0 and partner == 0
    assert plugin_api.failure_counts() == {"toy:on_answer": 1}


def test_a_second_plugins_hook_stays_quiet_for_the_first_plugins_source(db, world):
    seen = []

    def one(api):
        api.answer_source("chat-one", "One")
        api.on_answer("mark", lambda c, d, a: seen.append(("one", a.source)))

    def two(api):
        api.answer_source("chat-two", "Two")
        api.on_answer("mark", lambda c, d, a: seen.append(("two", a.source)))

    world(EP("one", _with_module(one, "1")), EP("two", _with_module(two, "1")))
    plugin_api.load()
    decisions.answer(db, _ask(db).id, letter="a", by="owner", source="chat-one")
    assert seen == [("one", "chat-one")]


def test_record_elsewhere_does_not_run_the_hook(db, world):
    seen = []

    def register(api):
        api.answer_source("toy-chat", "Toy", relay=True)
        api.on_answer("mark", lambda c, d, a: seen.append(a.source))

    world(EP("toy", _with_module(register, "1")))
    plugin_api.load()
    decisions.record_elsewhere(
        db,
        _ask(db).id,
        letter="a",
        note="said so",
        source="toy-chat",
        recorded_by="seat-a",
    )
    assert seen == []


@pytest.mark.parametrize("name", ["slack", "page"])
def test_a_plugin_cannot_register_slack_or_page(world, name):
    def register(api):
        api.answer_source(name, "Mine", relay=True)

    world(EP("toy", _with_module(register, "1")))
    with pytest.raises(plugin_api.PluginError, match="toy"):
        plugin_api.load()
    assert name not in decisions.relay_sources()


def test_an_unregistered_source_is_still_refused(db, world):
    world()
    with pytest.raises(decisions.InvalidSource):
        decisions.answer(db, _ask(db).id, letter="a", by="owner", source="nope")


def test_the_same_hook_name_twice_from_a_plugin_is_refused(world):
    def register(api):
        api.answer_source("toy-chat", "Toy")
        api.on_answer("mark", lambda *a: None)
        api.on_answer("mark", lambda *a: None)

    world(EP("toy", _with_module(register, "1")))
    with pytest.raises(plugin_api.PluginError, match="toy"):
        plugin_api.load()


def test_on_answer_needs_an_answer_source_first_and_stops_start_up(world):
    def register(api):
        api.on_answer("mark", lambda *a: None)

    world(EP("toy", _with_module(register, "1")))
    with pytest.raises(plugin_api.PluginError, match="toy"):
        plugin_api.load()


def test_a_sink_naming_an_unknown_kind_stops_start_up_naming_plugin_and_kind(world):
    def register(api):
        api.sink("typo", ("decision_anwered",), lambda e: None)

    world(EP("toy", _with_module(register, "1")))
    with pytest.raises(plugin_api.PluginError) as err:
        plugin_api.load()
    assert "toy" in str(err.value) and "decision_anwered" in str(err.value)
    assert sinks.matching("decision_answered") == []


def test_a_sink_may_name_journal_and_bridge_kinds(world):
    def register(api):
        api.sink("both", ("decision_answered", "asked"), lambda e: None)

    world(EP("toy", _with_module(register, "1")))
    assert plugin_api.load() == ["toy"]
    assert [s.name for s in sinks.matching("asked")] == ["both"]


def test_a_failed_answer_hook_is_a_503_json_naming_the_plugin(db, world):
    from fastapi.testclient import TestClient

    from ream.app import create_api_app, create_gallery_app

    for make in (create_api_app, create_gallery_app):
        app = make(db)

        @app.get("/_hook_fail")
        def _fail():
            raise decisions.AnswerHookFailed("toy", "mark")

        response = TestClient(app, raise_server_exceptions=False).get("/_hook_fail")
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert "toy" in detail and "mark" not in detail


def test_the_gallery_answer_form_shows_a_readable_page_when_a_hook_fails(
    db, world, monkeypatch
):
    from fastapi.testclient import TestClient

    from ream.app import create_gallery_app

    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    q = _ask(db)

    def boom(*args, **kwargs):
        raise decisions.AnswerHookFailed("toy", "zzhook")

    monkeypatch.setattr(decisions, "answer", boom)
    response = TestClient(create_gallery_app(db), base_url="https://testserver").post(
        f"/d/{q.id}/answer",
        data={"letter": "a"},
        headers={"Remote-User": "jeremy", "sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    assert "toy" in response.text and "not saved" in response.text
    assert "Traceback" not in response.text and "zzhook" not in response.text
