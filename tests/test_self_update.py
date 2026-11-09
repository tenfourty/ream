"""The tools say which level they are on and reinstall themselves when the service says they
are out of date (Jeremy, 9 Nov 2026). The install source is the client's own, never the server's."""

import io
import urllib.request

import pytest
from fastapi.testclient import TestClient

from ream import client, clientlevel, keys, selfupdate
from ream.app import create_app

SOURCE = "git+https://forge.example/runcove/artifacts"
STALE = {"x-ream-client-outdated": "true", "x-ream-client-ref": "v1.4.0"}


@pytest.fixture()
def http(db) -> TestClient:
    return TestClient(create_app(db), follow_redirects=False)


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
    monkeypatch.setenv("REAM_CLIENT_INSTALL_REF", "v1.4.0")
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
        assert response.headers["x-ream-client-ref"] == "v1.4.0"
        assert response.headers["x-ream-install-source"] == SOURCE


def test_a_current_tool_is_not_told_anything_is_wrong(http, db, monkeypatch):
    monkeypatch.delenv("REAM_CLIENT_INSTALL_REF", raising=False)
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
    assert "x-ream-client-ref" not in response.headers, "unset means none"
    assert "x-ream-install-source" not in response.headers
    assert response.headers["x-ream-client-min"] == str(clientlevel.MIN_CLIENT_LEVEL)


def test_the_signal_rides_an_error_answer_too_and_not_the_gallery(http):
    refused = http.get("/api/v1/pages")
    assert refused.status_code == 401
    assert refused.headers["x-ream-client-outdated"] == "true"
    assert "x-ream-client-min" not in http.get("/healthz").headers


def test_the_agent_card_declares_the_level(http, monkeypatch):
    monkeypatch.setenv("REAM_CLIENT_INSTALL_REF", "abcdef1")
    card = http.get("/api/v1/agent.json").json()["cli"]["client_level"]
    assert card["current"] == clientlevel.CLIENT_LEVEL
    assert card["minimum"] == clientlevel.MIN_CLIENT_LEVEL
    assert card["install_ref"] == "abcdef1"
    assert card["install_source"] is None


# ---- the tool decides to update ----------------------------------------------------


class _Run:
    def __init__(self, code=0):
        self.code = code
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return type("Done", (), {"returncode": self.code})()


@pytest.fixture()
def updater(monkeypatch):
    for name in (
        "REAM_NO_SELF_UPDATE",
        "ARTIFACTS_NO_SELF_UPDATE",
        "REAM_SELF_UPDATED",
        "ARTIFACTS_INSTALL_SOURCE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REAM_SELF_UPDATED", "x")  # so the test's own change is undone
    monkeypatch.delenv("REAM_SELF_UPDATED")
    monkeypatch.setenv("REAM_INSTALL_SOURCE", SOURCE)
    monkeypatch.setattr(selfupdate, "_done", False)
    monkeypatch.setattr(selfupdate.shutil, "which", lambda name: f"/bin/{name}")
    run = _Run()
    monkeypatch.setattr(selfupdate.subprocess, "run", run)
    execs = []
    monkeypatch.setattr(selfupdate.os, "execv", lambda *a: execs.append(a))
    monkeypatch.setattr(selfupdate, "_ORIGINAL_ARGV", ["/bin/ream", "list"])
    out = io.StringIO()
    monkeypatch.setattr(selfupdate.sys, "stderr", out)
    return type("Rig", (), {"run": run, "execs": execs, "out": out})


def test_it_reinstalls_from_the_clients_own_source_at_the_named_ref_then_repeats(
    updater,
):
    selfupdate.on_response("GET", STALE)
    command, kwargs = updater.run.calls[0]
    assert command == ["uv", "tool", "install", "--force", f"{SOURCE}@v1.4.0"]
    assert "shell" not in kwargs
    assert updater.execs == [("/bin/ream", ["/bin/ream", "list"])]
    assert selfupdate.os.environ["REAM_SELF_UPDATED"] == "1"
    selfupdate.os.environ.pop("REAM_SELF_UPDATED")


def test_a_source_in_the_servers_headers_is_never_used(updater):
    selfupdate.on_response(
        "GET",
        {**STALE, "x-ream-install-source": "git+https://evil.example/x"},
    )
    command = updater.run.calls[0][0]
    assert "evil.example" not in " ".join(command)
    assert command[-1].startswith(SOURCE + "@")
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)


@pytest.mark.parametrize(
    "ref",
    [
        "--index-url=https://evil.example",
        "v1.4.0 --extra",
        "main",
        "../x",
        "v1.4.0@evil",
        "git+https://evil.example/x",
        "abc",
        "",
        "v1.4.0\n",
    ],
)
def test_a_bad_ref_from_the_server_is_rejected(updater, ref):
    selfupdate.on_response(
        "GET", {"x-ream-client-outdated": "true", "x-ream-client-ref": ref}
    )
    assert updater.run.calls == []
    assert updater.execs == []
    assert "out of date" in updater.out.getvalue()


@pytest.mark.parametrize("ref", ["v1.4.0", "2026.11.9", "585ac56", "a" * 40])
def test_a_tag_or_a_commit_hash_is_accepted(ref):
    assert selfupdate.valid_ref(ref)


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
    assert len(updater.run.calls) == 1
    assert updater.execs == [], "a POST that went through must not be sent twice"
    assert "next call" in updater.out.getvalue()


def test_a_current_answer_does_nothing(updater):
    selfupdate.on_response("GET", {"x-ream-client-min": "1"})
    assert updater.run.calls == []


def test_it_asks_once_per_process(updater):
    selfupdate.on_response("POST", STALE)
    selfupdate.on_response("POST", STALE)
    assert len(updater.run.calls) == 1


# ---- the loop guard, the opt-out and the failures ----------------------------------


def test_the_loop_guard_stops_a_second_update(updater, monkeypatch):
    monkeypatch.setenv("REAM_SELF_UPDATED", "1")
    selfupdate.on_response("GET", STALE)
    assert updater.run.calls == []
    assert updater.execs == []
    assert f"{SOURCE}@v1.4.0" in updater.out.getvalue()


@pytest.mark.parametrize("name", ["REAM_NO_SELF_UPDATE", "ARTIFACTS_NO_SELF_UPDATE"])
def test_the_opt_out_only_prints_the_line(updater, monkeypatch, name):
    monkeypatch.setenv(name, "1")
    selfupdate.on_response("GET", STALE)
    assert updater.run.calls == []
    assert updater.execs == []
    assert f"uv tool install --force {SOURCE}@v1.4.0" in updater.out.getvalue()


def test_without_uv_it_prints_the_exact_command(updater, monkeypatch):
    monkeypatch.setattr(selfupdate.shutil, "which", lambda name: None)
    selfupdate.on_response("GET", STALE)
    assert updater.run.calls == []
    assert f"uv tool install --force {SOURCE}@v1.4.0" in updater.out.getvalue()
    assert "uv is not installed" in updater.out.getvalue()


def test_a_failed_install_prints_the_command_and_carries_on(updater):
    updater.run.code = 1
    selfupdate.on_response("GET", STALE)
    assert updater.execs == []
    assert f"uv tool install --force {SOURCE}@v1.4.0" in updater.out.getvalue()


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
