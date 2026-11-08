"""The plug-in text and router hooks (core-and-plugins design 4.2, slice 7b part 3): extra
template variables, named text slots in the two onboarding documents, and an inbound route on
a dedicated host. With no plug-in nothing is added; a plug-in fault never leaks its message."""

import itertools
import logging
import sys
import types
from urllib.parse import urlsplit

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from ream import hosts, onboarding, plugin_api, settings, sinks, store, textvars
from ream.app import create_app

_counter = itertools.count()
LEAK_MARK = "sekrit-payload-77"


class EP:
    def __init__(self, name, register):
        self.name = name
        self._register = register

    def load(self):
        return self._register


def _plugin(fn):
    mod = types.ModuleType(f"fake_text_mod_{next(_counter)}")
    fn.__module__ = mod.__name__
    sys.modules[mod.__name__] = mod
    return fn


@pytest.fixture()
def world(monkeypatch):
    monkeypatch.setattr(textvars, "_PLUGIN_VARS", {})
    monkeypatch.setattr(textvars, "_SECTIONS", [])
    monkeypatch.setattr(plugin_api, "_ROUTERS", {})
    monkeypatch.setattr(plugin_api, "_failures", {})
    monkeypatch.setattr(plugin_api, "_loaded", [])

    def install(*eps):
        monkeypatch.setattr(plugin_api, "_entry_points", lambda: list(eps))

    with sinks.isolated():
        yield install


def _load(world, name, register):
    world(EP(name, _plugin(register)))
    return plugin_api.load()


# --- variables ---------------------------------------------------------------------------


def test_with_no_plugin_the_documents_carry_no_slot_marker(world):
    for text in (onboarding.agents_txt("https://x.example"), onboarding.skill_md("x")):
        assert "section_" not in text
        assert "{{" not in text


def test_a_plugin_variable_renders_in_its_own_namespace(world):
    state = {"n": 0}

    def register(api):
        def current():
            state["n"] += 1
            return f"v{state['n']}"

        api.text_vars({"beads_page_label": "Beads", "beads_page_now": current})

    _load(world, "beads-page", register)
    assert textvars.render("{{beads_page_label}}/{{Beads_page_label}}") == "Beads/Beads"
    assert textvars.render("{{beads_page_now}}") == "v1"
    assert textvars.render("{{beads_page_now}}") == "v2"
    assert textvars.render("{{owner_name}}") == settings.current().owner_name


@pytest.mark.parametrize(
    "name", ["owner_name", "toy_Bad", "other_x", "base", "section_tools", "toy"]
)
def test_a_variable_outside_the_plugins_prefix_or_on_a_core_name_stops_start_up(
    world, name
):
    def register(api):
        api.text_vars({name: "x"})

    with pytest.raises(plugin_api.PluginError, match="toy"):
        _load(world, "toy", register)
    assert textvars.render("{{owner_name}}") != "x"


def test_a_prefix_is_not_a_core_name_even_for_a_plugin_called_owner(world):
    def register(api):
        api.text_vars({"owner_name": "mine"})

    with pytest.raises(plugin_api.PluginError, match="owner"):
        _load(world, "owner", register)


def test_two_plugins_cannot_share_a_variable(world):
    def one(api):
        api.text_vars({"a_b_x": "1"})

    def two(api):
        api.text_vars({"a_b_x": "2"})

    world(EP("a-b", _plugin(one)), EP("a_b", _plugin(two)))
    with pytest.raises(plugin_api.PluginError, match="a_b"):
        plugin_api.load()


def test_a_raising_or_non_string_variable_renders_empty_and_leaks_nothing(
    world, caplog
):
    def register(api):
        def boom():
            raise RuntimeError(LEAK_MARK)

        api.text_vars({"toy_boom": boom, "toy_num": lambda: 5})

    _load(world, "toy", register)
    with caplog.at_level(logging.DEBUG):
        assert textvars.render("[{{toy_boom}}][{{toy_num}}]") == "[][]"
    assert LEAK_MARK not in caplog.text
    assert "RuntimeError" in caplog.text
    assert plugin_api.failure_counts()["toy:text"] == 2


# --- sections ----------------------------------------------------------------------------


def test_sections_land_in_their_slots_and_documents(world):
    def one(api):
        api.text_section("tools", lambda doc: f"ONE-TOOLS {doc}")
        api.text_section(
            "proxy", lambda doc: "ONE-PROXY" if doc == "agents.txt" else ""
        )
        api.text_section(
            "integrations", lambda doc: "ONE-INT\n" if doc == "skill.md" else ""
        )

    def two(api):
        api.text_section("tools", lambda doc: "TWO-TOOLS")

    world(EP("one", _plugin(one)), EP("two", _plugin(two)))
    plugin_api.load()
    agents = onboarding.agents_txt("https://x.example")
    skill = onboarding.skill_md("https://x.example")
    assert "\nONE-TOOLS agents.txt\n\nTWO-TOOLS\n" in agents
    assert agents.index("ONE-TOOLS") < agents.index("If you would rather not install")
    assert "ONE-PROXY" in agents and "ONE-PROXY" not in skill
    assert "ONE-INT" not in agents and skill.endswith("\nONE-INT\n")
    assert "TOOLS" not in skill
    assert "{{" not in agents + skill


def test_a_section_naming_an_unknown_slot_or_twice_stops_start_up(world):
    def bad(api):
        api.text_section("header", lambda doc: "x")

    with pytest.raises(plugin_api.PluginError, match="toy"):
        _load(world, "toy", bad)

    def twice(api):
        api.text_section("tools", lambda doc: "x")
        api.text_section("tools", lambda doc: "y")

    with pytest.raises(plugin_api.PluginError, match="again"):
        _load(world, "again", twice)


def test_a_raising_section_is_left_out_and_leaks_nothing(world, caplog):
    def register(api):
        def boom(doc):
            raise ValueError(LEAK_MARK)

        api.text_section("tools", boom)
        api.text_section("integrations", lambda doc: 7)

    plain = onboarding.agents_txt("https://x.example")
    _load(world, "toy", register)
    with caplog.at_level(logging.DEBUG):
        assert onboarding.agents_txt("https://x.example") == plain
        assert onboarding.skill_md("https://x.example")
    assert LEAK_MARK not in caplog.text
    assert "ValueError" in caplog.text
    assert plugin_api.failure_counts()["toy:text"] == 3


def test_an_oversize_section_is_dropped(world):
    def register(api):
        api.text_section("tools", lambda doc: "x" * (textvars._MAX_PLUGIN_TEXT + 1))

    _load(world, "toy", register)
    assert textvars.section("tools", "agents.txt") == ""


# --- router ------------------------------------------------------------------------------


def _router():
    router = APIRouter()

    @router.get("/hook")
    def hook():
        return {"ok": True}

    @router.get("/boom")
    def boom():
        raise RuntimeError(LEAK_MARK)

    @router.post("/typed")
    def typed(n: int):
        return {"n": n}

    return router


def _client(db, host):
    return TestClient(
        create_app(db), base_url=f"http://{host}", raise_server_exceptions=False
    )


def test_a_router_is_served_only_on_its_host(db, world):
    _load(world, "toy", lambda api: api.router(_router(), "Hook.Example:8000"))
    assert plugin_api.routers().keys() == {"hook.example"}
    assert _client(db, "hook.example").get("/hook").json() == {"ok": True}
    assert _client(db, "hook.example").get("/docs").status_code == 404
    assert _client(db, "hook.example").get("/openapi.json").status_code == 404
    assert _client(db, "other.example").get("/hook").status_code == 404
    gallery = urlsplit(store.gallery_origin()).hostname
    assert _client(db, gallery).get("/hook").status_code == 404


def test_with_no_router_an_unknown_host_is_still_404(db, world):
    world()
    plugin_api.load()
    assert plugin_api.routers() == {}
    assert _client(db, "hook.example").get("/hook").status_code == 404


def test_a_fault_in_a_route_is_a_bare_500_counted_and_unlogged_in_content(
    db, world, caplog
):
    _load(world, "toy", lambda api: api.router(_router(), "hook.example"))
    with caplog.at_level(logging.DEBUG):
        r = _client(db, "hook.example").get("/boom")
    assert r.status_code == 500
    assert LEAK_MARK not in r.text and LEAK_MARK not in caplog.text
    assert plugin_api.failure_counts()["toy:router"] == 1
    assert "RuntimeError" in caplog.text


def test_a_validation_error_echoes_no_input(db, world):
    _load(world, "toy", lambda api: api.router(_router(), "hook.example"))
    r = _client(db, "hook.example").post(f"/typed?n={LEAK_MARK}")
    assert r.status_code == 422
    assert LEAK_MARK not in r.text


def _reserved_cases():
    return [
        ("gallery", lambda: urlsplit(store.gallery_origin()).hostname),
        ("api", lambda: "api.example"),
        ("partner", lambda: "svc.internal"),
        ("content", lambda: store.content_host()),
        ("content-sub", lambda: "x." + store.content_host()),
    ]


@pytest.mark.parametrize("label,host", _reserved_cases())
def test_a_router_may_not_take_a_reserved_host(db, world, label, host):
    with settings.override(api_host="api.example", partner_inbound_host="svc.internal"):
        wanted = host()
        assert wanted
        with pytest.raises(plugin_api.PluginError, match="toy"):
            _load(world, "toy", lambda api: api.router(_router(), wanted))
        assert plugin_api.routers() == {}
        assert hosts.normalise(wanted)


def test_a_router_host_is_not_shared_and_the_registration_must_be_well_formed(
    db, world
):
    def one(api):
        api.router(_router(), "hook.example")

    def two(api):
        api.router(_router(), "HOOK.example.")

    world(EP("one", _plugin(one)), EP("two", _plugin(two)))
    with pytest.raises(plugin_api.PluginError, match="two"):
        plugin_api.load()

    for bad in (
        lambda api: api.router(object(), "h.example"),
        lambda api: api.router(_router(), ""),
        lambda api: api.router(_router(), None),
    ):
        with pytest.raises(plugin_api.PluginError, match="bad"):
            _load(world, "bad", bad)
