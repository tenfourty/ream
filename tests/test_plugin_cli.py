"""The command-line side of the plug-in API (7b part 4): `api.cli`, `api.client_defaults`
and the CLI's loading of plug-ins. Plug-ins are loaded without installing a package:
`plugin_cli.entry_points` is replaced."""

import sys
from datetime import UTC, datetime

import pytest

from ream import cli, client, news, plugin_api, plugin_cli

TODAY = datetime.now(UTC).date().isoformat()


class EP:
    def __init__(self, name, register):
        self.name = name
        self._register = register

    def load(self):
        return self._register


@pytest.fixture()
def world(monkeypatch):
    plugin_cli.reset()
    # cli.main rewrites sys.argv for the verb it runs; put it back after the test.
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    for name in ("REAM_PLUGINS", "ARTIFACTS_PLUGINS", "REAM_API", "ARTIFACTS_API"):
        monkeypatch.delenv(name, raising=False)

    def install(*eps):
        monkeypatch.setattr(plugin_cli, "entry_points", lambda: list(eps))

    yield install
    plugin_cli.reset()


def test_with_no_plugin_the_help_has_no_plugin_section(world):
    world()
    plugin_cli.load_for_cli()
    assert "From plug-ins" not in cli._help()
    assert plugin_cli.commands() == []
    assert plugin_cli.default_api_url() is None
    assert plugin_cli.session_envs() == []
    assert plugin_cli.news() == []


def test_a_plugin_verb_runs_with_ream_argv_and_is_listed_in_help(
    world, monkeypatch, capsys
):
    seen = {}

    def main():
        seen["argv"] = list(__import__("sys").argv)
        return 3

    world(EP("toy", lambda api: api.cli("tracker", main, "a toy tracker page")))
    with pytest.raises(SystemExit) as stop:
        cli.main(["tracker", "--x", "1"])
    assert stop.value.code == 3
    assert seen["argv"] == ["ream tracker", "--x", "1"]
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "From plug-ins:" in out
    assert "tracker" in out and "a toy tracker page" in out


def test_a_plugin_verb_fault_prints_the_type_only(world, capsys):
    def main():
        raise RuntimeError("secret-value-123")

    world(EP("toy", lambda api: api.cli("boom", main, "boom")))
    with pytest.raises(SystemExit) as stop:
        cli.main(["boom"])
    assert stop.value.code == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "secret-value-123" not in err


@pytest.mark.parametrize(
    "verb",
    [
        "publish",
        "ask",
        "admin",
        "help",
        "connect",
        "republish",
        "beads-page",
        "bead-metrics",
        "Bad Name",
        "",
    ],
)
def test_a_plugin_may_not_take_a_reserved_or_malformed_verb(world, verb):
    world(EP("toy", lambda api: api.cli(verb, lambda: 0, "x")))
    with pytest.raises(plugin_cli.PluginError) as err:
        plugin_cli.load_for_cli()
    assert "'toy'" in str(err.value)


def test_two_plugins_cannot_share_a_verb(world):
    world(
        EP("a", lambda api: api.cli("same", lambda: 0, "x")),
        EP("b", lambda api: api.cli("same", lambda: 0, "y")),
    )
    with pytest.raises(plugin_cli.PluginError) as err:
        plugin_cli.load_for_cli()
    assert "'b'" in str(err.value)


def test_load_or_exit_stops_the_tool_naming_the_plugin(world, capsys):
    def bad(api):
        raise RuntimeError("leaky-text")

    world(EP("toy", bad))
    with pytest.raises(SystemExit) as stop:
        plugin_cli.load_or_exit()
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert "toy" in err and "RuntimeError" in err and "leaky-text" not in err


def test_the_plugin_selection_applies_to_the_cli(world, monkeypatch):
    world(EP("toy", lambda api: api.cli("tracker", lambda: 0, "x")))
    monkeypatch.setenv("REAM_PLUGINS", "none")
    plugin_cli.load_for_cli()
    assert plugin_cli.commands() == []
    plugin_cli.reset()
    monkeypatch.setenv("REAM_PLUGINS", "toy")
    plugin_cli.load_for_cli()
    assert [c.name for c in plugin_cli.commands()] == ["tracker"]
    plugin_cli.reset()
    monkeypatch.setenv("REAM_PLUGINS", "toy,missing")
    with pytest.raises(plugin_cli.PluginError):
        plugin_cli.load_for_cli()
    plugin_cli.reset()
    monkeypatch.delenv("REAM_PLUGINS")
    monkeypatch.setenv("ARTIFACTS_PLUGINS", "none")
    plugin_cli.load_for_cli()
    assert plugin_cli.commands() == []


def test_the_servers_registrations_are_ignored_by_the_cli(world):
    def register(api):
        api.sink("s", ("decision_answered",), lambda e: None)
        api.answer_source("src", "Src")
        api.on_answer("h", lambda *a: None)
        api.scanner("sc", lambda t: [])
        api.mail_backend("m", lambda *a: None, lambda: True)
        api.cli("tracker", lambda: 0, "x")

    world(EP("toy", register))
    assert plugin_cli.load_for_cli() == ["toy"]
    assert [c.name for c in plugin_cli.commands()] == ["tracker"]


def test_the_server_api_validates_cli_registrations_too(world):
    api = plugin_api.PluginAPI("toy")
    with pytest.raises(ValueError):
        api.cli("publish", lambda: 0, "x")
    api.cli("tracker", lambda: 0, "x")
    assert plugin_cli.command("tracker") is not None


def test_client_defaults_endpoint_applies_only_when_ream_api_is_unset(
    world, monkeypatch
):
    world(
        EP(
            "toy",
            lambda api: api.client_defaults(api_url="https://ream.example.org/api/v1"),
        )
    )
    plugin_cli.load_for_cli()
    assert client._api_base() == "https://ream.example.org/api/v1"
    monkeypatch.setenv("REAM_API", "http://localhost:9/api/v1")
    assert client._api_base() == "http://localhost:9/api/v1"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://x.example/api",
        "https://user:pw@x.example/api",
        "https://x.example/api?k=v",
        "https:///api",
        "not a url",
        7,
    ],
)
def test_a_bad_endpoint_is_refused_and_changes_nothing(world, url):
    world(EP("toy", lambda api: api.client_defaults(api_url=url, session_envs=["A"])))
    with pytest.raises(plugin_cli.PluginError):
        plugin_cli.load_for_cli()
    assert plugin_cli.default_api_url() is None
    assert plugin_cli.session_envs() == []


def test_only_one_plugin_supplies_the_endpoint(world):
    world(
        EP("a", lambda api: api.client_defaults(api_url="https://a.example/api")),
        EP("b", lambda api: api.client_defaults(api_url="https://b.example/api")),
    )
    with pytest.raises(plugin_cli.PluginError) as err:
        plugin_cli.load_for_cli()
    assert "'b'" in str(err.value)


def test_session_envs_are_asked_first_and_never_a_credential_name(world, monkeypatch):
    world(EP("toy", lambda api: api.client_defaults(session_envs=["TOY_OFFICE"])))
    plugin_cli.load_for_cli()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "48147c82-aaaa")
    monkeypatch.setenv("QD_OFFICE", "qd-office")
    monkeypatch.setenv("TOY_OFFICE", "toy-office")
    assert client.default_session() == "toy-office/48147c82"
    monkeypatch.delenv("TOY_OFFICE")
    assert client.default_session() == "qd-office/48147c82"
    plugin_cli.reset()
    for bad in ("TOY_API_KEY", "lower", "A B"):
        with pytest.raises(ValueError):
            plugin_cli.register_client_defaults("toy", session_envs=[bad])


def test_plugin_news_merges_newest_first_and_is_validated(world):
    world(
        EP(
            "toy",
            lambda api: api.client_defaults(
                news=[(TODAY, "from the plug-in"), ("2000-01-01", "old")]
            ),
        )
    )
    plugin_cli.load_for_cli()
    ids = [day for day, _ in news.entries()]
    assert ids[0] >= TODAY and ids[-1] == "2000-01-01"
    plugin_cli.reset()
    with pytest.raises(ValueError):
        plugin_cli.register_client_defaults("toy", news=[("yesterday", "x")])


def test_plugin_news_refuses_a_future_id_and_control_characters(world):
    for bad in (
        [("2999-01-01", "from the future")],
        [("2026-13-45", "not a date")],
        [(TODAY, "esc \x1b[31mred")],
        [(TODAY, "two\nlines")],
        [(TODAY, "cr\rhere")],
        [(TODAY, "c1 \x9bcsi")],
    ):
        with pytest.raises(ValueError):
            plugin_cli.register_client_defaults("toy", news=bad)
    assert plugin_cli.news() == []


# Default selection: a broken plug-in is skipped; a named one stops the tool.


def test_a_broken_plugin_is_skipped_with_a_warning_by_default(world, capsys):
    def bad(api):
        api.cli("half", lambda: 0, "registered before it failed")
        raise RuntimeError("leaky-text")

    world(EP("bad", bad), EP("good", lambda api: api.cli("fine", lambda: 0, "ok")))
    plugin_cli.load_or_exit()
    err = capsys.readouterr().err
    assert "'bad'" in err and "RuntimeError" in err and "leaky-text" not in err
    assert "REAM_PLUGINS=none" in err
    assert [c.name for c in plugin_cli.commands()] == ["fine"]


def test_a_failing_plugin_load_is_skipped_too(world, capsys):
    class Broken(EP):
        def load(self):
            raise ImportError("no module secret-path")

    world(Broken("bad", None))
    plugin_cli.load_or_exit()
    err = capsys.readouterr().err
    assert "'bad'" in err and "ImportError" in err and "secret-path" not in err


def test_a_named_broken_plugin_still_exits_2_with_the_hint(world, monkeypatch, capsys):
    def bad(api):
        raise RuntimeError("leaky-text")

    world(EP("bad", bad))
    monkeypatch.setenv("REAM_PLUGINS", "bad")
    with pytest.raises(SystemExit) as stop:
        plugin_cli.load_or_exit()
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert "'bad'" in err and "REAM_PLUGINS=none" in err and "leaky-text" not in err


def test_conflicting_plugin_settings_exit_2_without_a_value(world, monkeypatch, capsys):
    world()
    monkeypatch.setenv("REAM_PLUGINS", "aaa")
    monkeypatch.setenv("ARTIFACTS_PLUGINS", "bbb")
    with pytest.raises(SystemExit) as stop:
        plugin_cli.load_or_exit()
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert "REAM_PLUGINS" in err and "aaa" not in err and "bbb" not in err


def test_the_server_start_still_stops_on_a_broken_plugin(world):
    def bad(api):
        raise RuntimeError("x")

    with pytest.raises(plugin_cli.PluginError):
        plugin_cli.register_all(
            [EP("bad", bad)], "", plugin_api.PluginAPI, plugin_cli._log
        )


@pytest.mark.parametrize("exc", [SystemExit(3), KeyboardInterrupt()])
def test_exit_and_interrupt_in_register_become_a_plugin_error(world, exc):
    def bad(api):
        raise exc

    world(EP("bad", bad))
    with pytest.raises(plugin_cli.PluginError) as err:
        plugin_cli.register_all(
            [EP("bad", bad)], "bad", plugin_cli.CliPluginAPI, plugin_cli._log
        )
    assert type(exc).__name__ in str(err.value)


def test_help_with_no_plugin_is_the_text_it_always_was(world):
    world()
    plugin_cli.load_for_cli()
    assert "{" not in cli._help() and "\n\nThe old `artifacts-*`" in cli._help()


# api_url


def test_the_environment_and_settings_win_over_a_plugin_endpoint(world, monkeypatch):
    from ream import settings

    world(
        EP(
            "toy",
            lambda api: api.client_defaults(api_url="https://ream.example.org/api/v1"),
        )
    )
    plugin_cli.load_for_cli()
    monkeypatch.setenv("ARTIFACTS_API", "http://localhost:9/old")
    assert client._api_base() == "http://localhost:9/old"
    monkeypatch.delenv("ARTIFACTS_API")
    with settings.override(api_url="http://localhost:7/x"):
        assert client._api_base() == "http://localhost:7/x"


@pytest.mark.parametrize(
    "url",
    [
        "http://ream.example.org/api",
        "https://x.example:0/api",
        "https://x.example:99999/api",
        "https://x.example:abc/api",
    ],
)
def test_an_unsafe_endpoint_is_refused_at_registration(world, url):
    with pytest.raises(ValueError):
        plugin_cli.register_client_defaults("toy", api_url=url)
    assert plugin_cli.default_api_url() is None


def test_a_loopback_http_endpoint_is_fine_and_a_trailing_slash_goes(world):
    plugin_cli.register_client_defaults("toy", api_url="http://127.0.0.1:8000/api/v1//")
    assert plugin_cli.default_api_url() == "http://127.0.0.1:8000/api/v1"


# session label


def test_a_plugin_session_value_must_be_a_plain_label(world, monkeypatch):
    world(EP("toy", lambda api: api.client_defaults(session_envs=["TOY_A", "TOY_B"])))
    plugin_cli.load_for_cli()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "48147c82-aaaa")
    monkeypatch.setenv("QD_OFFICE", "qd-office")
    monkeypatch.setenv("TOY_A", "bad label/with\x1b")
    assert client.default_session() == "qd-office/48147c82"
    monkeypatch.setenv("TOY_B", "Good.Label_1")
    assert client.default_session() == "good.label_1/48147c82"
    monkeypatch.setenv("TOY_B", "x" * 65)
    assert client.default_session() == "qd-office/48147c82"


# every entry point loads the plug-ins


def test_every_cli_entry_point_loads_the_plugins(monkeypatch):
    from ream import connect_cli, decide

    calls = []
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(plugin_cli, "load_or_exit", lambda: calls.append("load"))
    monkeypatch.setattr(news, "announce", lambda *a, **k: None)
    for module in (client, decide, connect_cli):
        monkeypatch.setattr(module, "main", lambda *a, **k: 0)
        with pytest.raises(SystemExit):
            module.run()
    assert calls == ["load"] * 3
    monkeypatch.setattr(sys, "argv", ["ream", "--help"])
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert calls == ["load"] * 4


def test_a_global_flag_may_come_before_a_plugin_verb(world, monkeypatch):
    seen = {}

    def main():
        seen["argv"] = list(sys.argv)
        return 0

    world(EP("toy", lambda api: api.cli("tracker", main, "x")))
    with pytest.raises(SystemExit):
        cli.main(["--session", "me/abc", "tracker", "--y"])
    assert seen["argv"] == ["ream tracker", "--session", "me/abc", "--y"]
