"""The old names keep working for good: `import artifacts`, `ARTIFACTS_*`, `X-Artifacts-*`, the
`artifacts.plugins` group and the `artifacts-*` commands (docs/decisions.md, "Ream inside the
code too"). This file reaches the code through the OLD names on purpose; the rest of the suite
uses the new ones."""

import importlib
import sys
from importlib import resources

import artifacts.settings as old_settings
import pytest
from artifacts.app import create_gallery_app
from artifacts.db import Database as OldDatabase
from fastapi.testclient import TestClient

import artifacts
import ream
import ream.db
import ream.settings
from artifacts import envnames as old_envnames
from artifacts import keys as old_keys

# --- the import name -----------------------------------------------------------------------


def test_the_old_package_is_the_new_one():
    assert artifacts is ream
    assert "ream" in sys.modules and sys.modules["artifacts"] is sys.modules["ream"]


def test_an_old_submodule_is_the_same_module_object_not_a_copy():
    assert old_settings is ream.settings
    assert OldDatabase is ream.db.Database
    assert sys.modules["artifacts.settings"] is sys.modules["ream.settings"]
    assert importlib.import_module("artifacts.db") is ream.db


def test_state_set_through_one_name_is_seen_through_the_other():
    with old_settings.override(footer="through the old name"):
        assert ream.settings.current().footer == "through the old name"


def test_a_module_that_does_not_exist_is_still_an_import_error():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("artifacts.no_such_module")


def test_resources_find_the_schema_under_the_old_name():
    assert (resources.files("artifacts") / "schema.sql").is_file()
    assert (resources.files("ream") / "schema.sql").is_file()


# --- the setting names ---------------------------------------------------------------------


@pytest.fixture()
def bare(monkeypatch):
    for name in ("REAM_DISPLAY_TZ", "ARTIFACTS_DISPLAY_TZ"):
        monkeypatch.delenv(name, raising=False)


def test_only_the_old_name_set_is_used(bare, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Asia/Tokyo")
    assert ream.settings.current().display_tz == "Asia/Tokyo"
    assert ream.settings.is_set("display_tz")


def test_only_the_new_name_set_is_used(bare, monkeypatch):
    monkeypatch.setenv("REAM_DISPLAY_TZ", "Asia/Tokyo")
    assert ream.settings.current().display_tz == "Asia/Tokyo"


def test_both_names_set_to_the_same_value_is_fine(bare, monkeypatch):
    monkeypatch.setenv("REAM_DISPLAY_TZ", "Asia/Tokyo")
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Asia/Tokyo")
    assert ream.settings.current().display_tz == "Asia/Tokyo"


def test_a_blank_name_is_unset(bare, monkeypatch):
    monkeypatch.setenv("REAM_DISPLAY_TZ", "  ")
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Asia/Tokyo")
    assert ream.settings.current().display_tz == "Asia/Tokyo"


def test_both_names_set_and_different_refuses_naming_both_and_no_value(
    bare, monkeypatch
):
    monkeypatch.setenv("REAM_DISPLAY_TZ", "value-one-secret")
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "value-two-secret")
    with pytest.raises(SystemExit) as refused:
        ream.settings.current()
    message = str(refused.value)
    assert "REAM_DISPLAY_TZ" in message and "ARTIFACTS_DISPLAY_TZ" in message
    assert "secret" not in message
    with pytest.raises(old_envnames.EnvConflict):
        old_envnames.get("DISPLAY_TZ")


def test_the_server_refuses_to_start_on_a_conflict(bare, monkeypatch):
    monkeypatch.setenv("REAM_DISPLAY_TZ", "Asia/Tokyo")
    monkeypatch.setenv("ARTIFACTS_DISPLAY_TZ", "Europe/Paris")
    with pytest.raises(SystemExit):
        ream.settings.validate_for_serve()


def test_a_child_gets_both_names_of_a_setting():
    env: dict[str, str] = {"ARTIFACTS_KEY_FD": "old"}
    old_envnames.child_env(env, "KEY_FD", "7")
    assert env == {"REAM_KEY_FD": "7", "ARTIFACTS_KEY_FD": "7"}
    old_envnames.child_env(env, "KEY_FD", None)
    assert env == {}


def test_a_command_line_tool_reads_the_old_name_too(monkeypatch, tmp_path):
    from ream import client

    key = tmp_path / "key"
    for name in ("REAM_KEY_FILE", "ARTIFACTS_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARTIFACTS_KEY_FILE", str(key))
    assert client._key_file_path(None) == key
    monkeypatch.setenv("REAM_KEY_FILE", str(key))
    assert client._key_file_path(None) == key
    monkeypatch.setenv("REAM_KEY_FILE", str(tmp_path / "other"))
    with pytest.raises(SystemExit):
        client._key_file_path(None)


# --- the headers ---------------------------------------------------------------------------


@pytest.fixture()
def client(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def auth(db):
    token, _ = old_keys.mint(db, agent="seat")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        ({"x-ream-session": "cove/new"}, "cove/new"),
        ({"x-artifacts-session": "cove/old"}, "cove/old"),
        ({"x-ream-session": "cove/new", "x-artifacts-session": "cove/old"}, "cove/new"),
    ],
)
def test_the_session_header_under_either_name(client, auth, sent, expected):
    got = client.get("/api/v1/me", headers={**auth, **sent})
    assert got.json()["session"] == expected


def test_pagination_headers_come_back_under_both_names(client, auth):
    got = client.get("/api/v1/events?since=999999", headers=auth)
    for suffix in ("cursor", "seen", "max-wait"):
        assert got.headers[f"x-ream-{suffix}"] == got.headers[f"x-artifacts-{suffix}"]


def test_the_header_helper_adds_the_old_twin_of_each():
    from ream import api

    assert api._both_headers({"x-ream-truncated": "true", "x-ream-next": "id1"}) == {
        "x-ream-truncated": "true",
        "x-ream-next": "id1",
        "x-artifacts-truncated": "true",
        "x-artifacts-next": "id1",
    }


def test_the_client_reads_either_name_of_a_response_header():
    from ream import decide

    assert decide._header({"x-ream-next": "a"}, "next") == "a"
    assert decide._header({"x-artifacts-next": "b"}, "next") == "b"
    assert decide._header({"x-ream-next": "a", "x-artifacts-next": "b"}, "next") == "a"
    assert decide._header({}, "next") is None


# --- plug-ins, the tool package, the commands ----------------------------------------------


class _Entry:
    def __init__(self, name, tag):
        self.name = name
        self.tag = tag


def test_plugins_are_found_in_both_groups_and_the_new_group_wins(monkeypatch):
    from ream import plugin_api

    groups = {
        "artifacts.plugins": [_Entry("old-only", "old"), _Entry("both", "old")],
        "ream.plugins": [_Entry("new-only", "new"), _Entry("both", "new")],
    }
    monkeypatch.setattr(
        plugin_api.metadata, "entry_points", lambda group: groups.get(group, [])
    )
    found = {ep.name: ep.tag for ep in plugin_api._entry_points()}
    assert found == {"old-only": "old", "both": "new", "new-only": "new"}


def test_the_tool_package_is_found_by_its_new_or_old_file_name(tmp_path, monkeypatch):
    from ream import onboarding

    monkeypatch.setenv("REAM_TOOLS_DIR", str(tmp_path))
    assert onboarding.wheel() is None
    old = tmp_path / "artifacts-0.1.0-py3-none-any.whl"
    old.write_bytes(b"x")
    assert onboarding.wheel() == old
    new = tmp_path / "ream-0.1.0-py3-none-any.whl"
    new.write_bytes(b"x")
    assert onboarding.wheel() == new


def test_the_ream_command_has_every_verb_of_the_old_ones():
    import argparse

    from ream import cli, decide

    sub = next(
        a
        for a in decide.build_parser()._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    assert set(cli.DECIDE_VERBS) == set(sub.choices)


def test_ream_help_exits_clean(capsys):
    from ream import cli

    with pytest.raises(SystemExit) as done:
        cli.main(["--help"])
    assert done.value.code == 0
    assert "publish" in capsys.readouterr().out


def test_an_unknown_verb_is_refused():
    from ream import cli

    with pytest.raises(SystemExit) as done:
        cli.main(["nonsense"])
    assert done.value.code == 2


def test_ream_publish_runs_the_publish_program(monkeypatch, capsys):
    from ream import cli

    monkeypatch.setattr(sys, "argv", ["ream"])
    with pytest.raises(SystemExit) as done:
        cli.main(["publish", "--help"])
    assert done.value.code == 0
    assert "FILE" in capsys.readouterr().out
    assert sys.argv[0] == "ream publish"


def test_ream_wait_reaches_the_decide_program(monkeypatch, capsys):
    from ream import cli

    monkeypatch.setattr(sys, "argv", ["ream"])
    with pytest.raises(SystemExit) as done:
        cli.main(["wait", "--help"])
    assert done.value.code == 0
    assert "--timeout" in capsys.readouterr().out
