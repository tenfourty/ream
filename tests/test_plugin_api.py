"""The plug-in API and loader (core-and-plugins design, slice 7a): a throwaway
in-tree plug-in for the sink and the answer source, and the failure cases.
Plug-ins are loaded without installing a package: `_entry_points` is replaced."""

import itertools
import sys
import types

import pytest

from artifacts import decisions, events, plugin_api, settings, sinks


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
        assert api.version == plugin_api.API_VERSION == (1, 0)
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
