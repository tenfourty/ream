"""The tools say which level they are on and reinstall themselves when the service says they
are out of date (Jeremy, 9 Nov 2026). The install source is the client's own, never the server's."""

import os
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from ream import client, clientlevel, keys, selfupdate
from ream.app import create_app

SOURCE = "git+https://forge.example/runcove/artifacts"
STALE = {"x-ream-client-outdated": "true"}


@pytest.fixture()
def http(db) -> TestClient:
    return TestClient(
        create_app(db),
        base_url="https://artifacts.tenfourty.site",
        follow_redirects=False,
    )


# ---- the tool says its level -------------------------------------------------------


def test_every_request_carries_the_level_under_both_names():
    request = urllib.request.Request("https://example.invalid/api/v1/me")
    client._add_client_level(request)
    level = str(clientlevel.CLIENT_LEVEL)
    assert request.get_header("X-ream-client") == level
    assert request.get_header("X-artifacts-client") == level


def test_the_json_and_raw_request_builders_send_it(monkeypatch):
    seen = []

    def fake_exchange(request, timeout, headers_out):
        seen.append(request)
        return b"{}"

    monkeypatch.setattr(client, "_exchange", fake_exchange)
    client._request("GET", "https://example.invalid/api/v1/me", "k", None)
    seen.append(client._raw_request("POST", "https://example.invalid/api/v1/x", "k"))
    for request in seen:
        assert request.get_header("X-ream-client") == str(clientlevel.CLIENT_LEVEL)
        assert request.get_header("X-artifacts-client") == str(clientlevel.CLIENT_LEVEL)


# ---- the server says what it needs -------------------------------------------------


def test_an_old_tool_is_answered_normally_and_told_it_is_out_of_date(
    http, db, monkeypatch
):
    monkeypatch.setenv("REAM_CLIENT_INSTALL_SOURCE", SOURCE)
    token, _ = keys.mint(db, agent="seat")
    auth = {"Authorization": f"Bearer {token}"}
    for extra in ({}, {"X-Ream-Client": "0"}, {"X-Artifacts-Client": "junk"}):
        response = http.get("/api/v1/pages", headers={**auth, **extra})
        assert response.status_code == 200, "still answered as usual"
        assert response.headers["x-ream-client-outdated"] == "true"
        assert response.headers["x-artifacts-client-outdated"] == "true"
        assert response.headers["x-ream-client-min"] == str(
            clientlevel.MIN_CLIENT_LEVEL
        )
        assert response.headers["x-artifacts-client-min"] == str(
            clientlevel.MIN_CLIENT_LEVEL
        )
        assert "x-ream-client-ref" not in response.headers, "the server names no ref"
        assert response.headers["x-ream-install-source"] == SOURCE


def test_a_current_tool_is_not_told_anything_is_wrong(http, db, monkeypatch):
    monkeypatch.delenv("REAM_CLIENT_INSTALL_SOURCE", raising=False)
    token, _ = keys.mint(db, agent="seat")
    response = http.get(
        "/api/v1/pages",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Ream-Client": str(clientlevel.CLIENT_LEVEL),
        },
    )
    assert "x-ream-client-outdated" not in response.headers
    assert "x-ream-install-source" not in response.headers
    assert response.headers["x-ream-client-min"] == str(clientlevel.MIN_CLIENT_LEVEL)


def test_the_signal_rides_an_error_answer_too_and_not_the_gallery(http):
    refused = http.get("/api/v1/pages")
    assert refused.status_code == 401
    assert refused.headers["x-ream-client-outdated"] == "true"
    assert "x-ream-client-min" not in http.get("/healthz").headers


def test_the_agent_card_declares_the_level(http, monkeypatch):
    assert "client_level" not in http.get("/api/v1/agent.json").json(), (
        "unset: unchanged"
    )
    monkeypatch.setenv("REAM_CLIENT_INSTALL_SOURCE", SOURCE)
    card = http.get("/api/v1/agent.json").json()["client_level"]
    assert card["current"] == clientlevel.CLIENT_LEVEL
    assert card["minimum"] == clientlevel.MIN_CLIENT_LEVEL
    assert "install_ref" not in card
    assert card["install_source"] == SOURCE


# ---- the tool decides to update ----------------------------------------------------

MAIN_COMMAND = ["uv", "tool", "install", "--force", "--refresh", f"{SOURCE}@main"]


class _Run:
    """Stands in for subprocess.run: the install, `uv tool dir`, then the installed Python."""

    def __init__(self):
        self.install_code = 0
        self.level = str(clientlevel.CLIENT_LEVEL)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        out, code = "", 0
        if command[:3] == ["uv", "tool", "install"]:
            code = self.install_code
        elif command[:3] == ["uv", "tool", "dir"]:
            out = "/tools\n"
        elif command[0].endswith("/python"):
            out = self.level + "\n"
        return type("Done", (), {"returncode": code, "stdout": out})()

    @property
    def installs(self):
        return [c for c, _ in self.calls if c[:3] == ["uv", "tool", "install"]]


class _Out:
    """What the tool printed to stderr so far (capsys, read as often as asked)."""

    def __init__(self, capsys):
        self.capsys = capsys
        self.text = ""

    def getvalue(self):
        self.text += self.capsys.readouterr().err
        return self.text


@pytest.fixture()
def updater(monkeypatch, tmp_path, capsys):
    for name in (
        "REAM_NO_SELF_UPDATE",
        "ARTIFACTS_NO_SELF_UPDATE",
        "ARTIFACTS_INSTALL_SOURCE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REAM_SELF_UPDATED", "x")  # so the test's own change is undone
    monkeypatch.delenv("REAM_SELF_UPDATED")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("REAM_INSTALL_SOURCE", SOURCE)
    monkeypatch.setattr(selfupdate, "_done", False)
    monkeypatch.setattr(
        selfupdate.shutil,
        "which",
        lambda name: name if name.startswith("/") else f"/bin/{name}",
    )
    run = _Run()
    monkeypatch.setattr(selfupdate.subprocess, "run", run)
    execs = []
    monkeypatch.setattr(selfupdate.os, "execv", lambda *a: execs.append(a))
    monkeypatch.setattr(selfupdate, "_ORIGINAL_ARGV", ["/bin/ream", "list"])
    out = _Out(capsys)
    return type("Rig", (), {"run": run, "execs": execs, "out": out})


def test_it_reinstalls_main_of_the_clients_own_source_then_repeats(updater):
    selfupdate.on_response("GET", STALE)
    assert updater.run.installs == [MAIN_COMMAND]
    assert all("shell" not in kw for _, kw in updater.run.calls)
    assert updater.execs == [("/bin/ream", ["/bin/ream", "list"])]
    assert selfupdate.os.environ["REAM_SELF_UPDATED"] == "1"
    selfupdate.os.environ.pop("REAM_SELF_UPDATED")


def test_nothing_the_server_sends_chooses_what_is_installed(updater):
    selfupdate.on_response(
        "GET",
        {
            **STALE,
            "x-ream-client-ref": "deadbeefcafe",
            "x-ream-install-source": "git+https://evil.example/x",
            "x-ream-client-min": "99",
        },
    )
    assert updater.run.installs == [MAIN_COMMAND], "main of the client's own source"
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)


def test_a_source_that_is_not_a_https_git_address_is_not_used(updater, monkeypatch):
    monkeypatch.setenv("REAM_INSTALL_SOURCE", "https://evil.example/x.whl")
    selfupdate.on_response("GET", STALE)
    assert updater.run.calls == []


def test_the_source_can_be_the_clients_config_file(updater, monkeypatch, tmp_path):
    monkeypatch.delenv("REAM_INSTALL_SOURCE")
    (tmp_path / "ream").mkdir()
    (tmp_path / "ream" / "install-source").write_text(SOURCE + "\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert selfupdate.configured_source() == SOURCE


def test_with_no_source_it_prints_a_line_and_installs_nothing(updater, monkeypatch):
    monkeypatch.delenv("REAM_INSTALL_SOURCE")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent")
    selfupdate.on_response("GET", STALE)
    assert updater.run.calls == []
    assert "out of date" in updater.out.getvalue()


def test_a_call_that_changes_something_is_not_repeated(updater):
    selfupdate.on_response("POST", STALE)
    assert len(updater.run.installs) == 1
    assert updater.execs == [], "a POST that went through must not be sent twice"
    assert "next call" in updater.out.getvalue()


def test_a_current_answer_does_nothing(updater):
    selfupdate.on_response("GET", {"x-ream-client-min": "1"})
    assert updater.run.calls == []


def test_it_asks_once_per_process(updater):
    selfupdate.on_response("POST", STALE)
    selfupdate.on_response("POST", STALE)
    assert len(updater.run.installs) == 1


# ---- no downgrade, the rate limit, the loop guard, the opt-out and the failures ----


def test_a_downgrade_is_refused_and_the_old_tool_carries_on(updater):
    updater.run.level = str(clientlevel.CLIENT_LEVEL - 1)
    selfupdate.on_response("GET", STALE)
    assert updater.execs == []
    assert " ".join(MAIN_COMMAND) in updater.out.getvalue()


def test_an_unreadable_installed_level_is_treated_as_a_downgrade(updater):
    updater.run.level = "not a number"
    selfupdate.on_response("GET", STALE)
    assert updater.execs == []


def test_at_most_one_attempt_an_hour_per_machine(updater, monkeypatch):
    selfupdate.on_response("POST", STALE)
    assert len(updater.run.installs) == 1
    monkeypatch.setattr(selfupdate, "_done", False)  # the next call, a new process
    selfupdate.on_response("POST", STALE)
    assert len(updater.run.installs) == 1, "a second attempt within the hour"
    assert " ".join(MAIN_COMMAND) in updater.out.getvalue()
    stamp = selfupdate._stamp_path()
    old = time.time() - selfupdate._MIN_GAP - 5
    os.utime(stamp, (old, old))
    monkeypatch.setattr(selfupdate, "_done", False)
    selfupdate.on_response("POST", STALE)
    assert len(updater.run.installs) == 2, "an hour later it may try again"


def test_a_failed_attempt_counts_against_the_hour_too(updater, monkeypatch):
    updater.run.install_code = 1
    selfupdate.on_response("GET", STALE)
    monkeypatch.setattr(selfupdate, "_done", False)
    selfupdate.on_response("GET", STALE)
    assert len(updater.run.installs) == 1


def test_the_loop_guard_stops_a_second_update(updater, monkeypatch):
    monkeypatch.setenv("REAM_SELF_UPDATED", "1")
    selfupdate.on_response("GET", STALE)
    assert updater.run.calls == []
    assert updater.execs == []
    assert " ".join(MAIN_COMMAND) in updater.out.getvalue()


@pytest.mark.parametrize("name", ["REAM_NO_SELF_UPDATE", "ARTIFACTS_NO_SELF_UPDATE"])
def test_the_opt_out_only_prints_the_line(updater, monkeypatch, name):
    monkeypatch.setenv(name, "1")
    selfupdate.on_response("GET", STALE)
    assert updater.run.calls == []
    assert updater.execs == []
    assert " ".join(MAIN_COMMAND) in updater.out.getvalue()


def test_without_uv_it_prints_the_exact_command(updater, monkeypatch):
    monkeypatch.setattr(selfupdate.shutil, "which", lambda name: None)
    selfupdate.on_response("GET", STALE)
    assert updater.run.calls == []
    assert " ".join(MAIN_COMMAND) in updater.out.getvalue()
    assert "uv is not installed" in updater.out.getvalue()


def test_a_failed_install_prints_the_command_and_carries_on(updater):
    updater.run.install_code = 1
    selfupdate.on_response("GET", STALE)
    assert updater.execs == []
    assert " ".join(MAIN_COMMAND) in updater.out.getvalue()


def test_an_install_that_cannot_start_prints_the_command(updater, monkeypatch):
    def boom(*a, **k):
        raise OSError("no")

    monkeypatch.setattr(selfupdate.subprocess, "run", boom)
    selfupdate.on_response("GET", STALE)
    assert updater.execs == []
    assert "Run by hand" in updater.out.getvalue()


def test_no_key_is_ever_printed(updater, monkeypatch):
    monkeypatch.setenv("REAM_KEY", "sekrit-key-value")
    selfupdate.on_response("GET", STALE)
    assert "sekrit" not in updater.out.getvalue()
    assert all("sekrit" not in " ".join(c) for c, _ in updater.run.calls)
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)


def test_the_exchange_hands_the_answers_headers_to_the_updater(monkeypatch):
    seen = []
    monkeypatch.setattr(selfupdate, "on_response", lambda m, h: seen.append((m, h)))

    class _Opener:
        def open(self, request, timeout=None):
            class _R:
                headers = {"X-Ream-Client-Outdated": "true"}  # noqa: RUF012

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                def read(self_inner):
                    return b"{}"

            return _R()

    monkeypatch.setattr(client, "_opener", _Opener())
    client._request("GET", "https://example.invalid/api/v1/me", "k", None)
    assert seen == [("GET", {"x-ream-client-outdated": "true"})]
