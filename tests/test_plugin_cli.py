"""The command-line side of the plug-in API (7b part 4): `api.cli`, `api.client_defaults`
and the CLI's loading of plug-ins. Plug-ins are loaded without installing a package:
`plugin_cli.entry_points` is replaced."""

import pytest

from ream import cli, client, news, plugin_api, plugin_cli


class EP:
    def __init__(self, name, register):
        self.name = name
        self._register = register

    def load(self):
        return self._register


@pytest.fixture()
def world(monkeypatch):
    plugin_cli.reset()
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


@pytest.mark.parametrize("verb", ["publish", "ask", "admin", "help", "Bad Name", ""])
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
                news=[("2999-01-01", "from the plug-in"), ("2000-01-01", "old")]
            ),
        )
    )
    plugin_cli.load_for_cli()
    ids = [day for day, _ in news.entries()]
    assert ids[0] == "2999-01-01" and ids[-1] == "2000-01-01"
    plugin_cli.reset()
    with pytest.raises(ValueError):
        plugin_cli.register_client_defaults("toy", news=[("yesterday", "x")])
