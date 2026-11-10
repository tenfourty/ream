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
        self.install_stderr = ""
        self.level = str(clientlevel.CLIENT_LEVEL)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        out, err, code = "", "", 0
        if command[:3] == ["uv", "tool", "install"]:
            code = self.install_code
            err = self.install_stderr
        elif command[:3] == ["uv", "tool", "dir"]:
            out = "/tools\n"
        elif command[0].endswith("/python"):
            out = self.level + "\n"
        return type("Done", (), {"returncode": code, "stdout": out, "stderr": err})()

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
    monkeypatch.setattr(selfupdate, "_mutated", False)
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
    assert "your next command uses the new tool" in updater.out.getvalue()


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
    monkeypatch.setattr(
        selfupdate, "on_response", lambda m, h, u=None: seen.append((m, h, u))
    )

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
    assert seen == [
        ("GET", {"x-ream-client-outdated": "true"}, "https://example.invalid/api/v1/me")
    ]


def _receipt(tmp_path, monkeypatch, text):
    env = tmp_path / "toolenv"
    env.mkdir(exist_ok=True)
    if text is not None:
        (env / "uv-receipt.toml").write_text(text)
    monkeypatch.setattr(selfupdate.sys, "prefix", str(env))
    monkeypatch.delenv("REAM_INSTALL_SOURCE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nocfg"))


GIT = 'git = "https://forge.example/runcove/artifacts@v1"'


def test_a_git_receipt_gives_its_address_without_the_ref(
    updater, tmp_path, monkeypatch
):
    _receipt(
        tmp_path, monkeypatch, f'[tool]\nrequirements = [{{ name = "ream", {GIT} }}]\n'
    )
    assert selfupdate.configured_source() == SOURCE
    selfupdate.on_response("GET", STALE)
    assert updater.run.installs == [MAIN_COMMAND]
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)


@pytest.mark.parametrize(
    "text",
    [
        '[tool]\nrequirements = [{ name = "ream", directory = "/home/x/repos/artifacts" }]\n',
        None,
        "this is [not toml",
        "[tool]\nrequirements = 5\n",
        '[tool]\nrequirements = [{ name = "ream", git = "http://forge.example/x" }]\n',
    ],
)
def test_a_path_missing_or_malformed_receipt_gives_no_source(
    updater, tmp_path, monkeypatch, text
):
    _receipt(tmp_path, monkeypatch, text)
    assert selfupdate.configured_source() is None


# ---- a tool installed from the tools index updates from that same index -------------
#
# What uv 0.13 writes for `uv tool install --force --refresh --no-index --find-links
# https://artifacts.tenfourty.site/api/v1/tools/ ream` (read from a real install into a
# throwaway tool directory): the requirement is the bare name, and the index rides in
# `[tool.options]`. Nothing in it names a git address or a path.

INDEX = "https://artifacts.tenfourty.site/api/v1/tools/"
INDEX_RECEIPT = (
    '[tool]\nrequirements = [{ name = "ream" }]\n'
    "entrypoints = [\n"
    '    { name = "ream", install-path = "/home/x/.local/bin/ream", from = "ream" },\n'
    "]\n\n"
    f'[tool.options]\nno-index = true\nfind-links = ["{INDEX}"]\n'
)
INDEX_COMMAND = [
    "uv",
    "tool",
    "install",
    "--force",
    "--refresh",
    "--no-index",
    "--find-links",
    INDEX,
    "ream",
]


def _index_receipt(
    requirement='{ name = "ream" }', links=f'["{INDEX}"]', no_index="true"
):
    """A receipt for a tool with `requirement` and the given `[tool.options]` values (raw
    TOML; None leaves the key out)."""
    lines = []
    if no_index is not None:
        lines.append(f"no-index = {no_index}")
    if links is not None:
        lines.append(f"find-links = {links}")
    return (
        f"[tool]\nrequirements = [{requirement}]\n\n[tool.options]\n"
        + "\n".join(lines)
        + "\n"
    )


def test_a_find_links_receipt_reinstalls_from_the_same_index(
    updater, tmp_path, monkeypatch
):
    _receipt(tmp_path, monkeypatch, INDEX_RECEIPT)
    selfupdate.on_response("GET", STALE)
    assert updater.run.installs == [INDEX_COMMAND]
    assert updater.execs == [("/bin/ream", ["/bin/ream", "list"])]
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)


def test_the_find_links_install_never_asks_a_public_index(
    updater, tmp_path, monkeypatch
):
    """The distribution name `ream` is an unrelated package on PyPI: the name only ever goes
    out beside `--no-index`, and the index is the receipt's, not the server's."""
    _receipt(tmp_path, monkeypatch, INDEX_RECEIPT)
    selfupdate.on_response(
        "GET",
        {**STALE, "x-ream-install-source": "git+https://evil.example/x"},
        "https://evil.example/api/v1/me",
    )
    (command,) = updater.run.installs
    assert command.index("--no-index") < command.index("ream")
    assert command[-1] == "ream" and command[-3:-1] == ["--find-links", INDEX]
    assert "evil.example" not in " ".join(command)
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)


def test_every_find_links_address_of_the_receipt_is_kept(
    updater, tmp_path, monkeypatch
):
    other = "https://second.example/tools/"
    _receipt(
        tmp_path,
        monkeypatch,
        _index_receipt(links=f'["{INDEX}", "{other}"]'),
    )
    selfupdate.on_response("GET", STALE)
    assert updater.run.installs == [
        [
            *INDEX_COMMAND[:-3],
            "--find-links",
            INDEX,
            "--find-links",
            other,
            "ream",
        ]
    ]
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)


def test_the_opt_out_prints_the_find_links_command_and_runs_nothing(
    updater, tmp_path, monkeypatch
):
    _receipt(tmp_path, monkeypatch, INDEX_RECEIPT)
    monkeypatch.setenv("REAM_NO_SELF_UPDATE", "1")
    selfupdate.on_response("GET", STALE)
    assert updater.run.calls == []
    assert " ".join(INDEX_COMMAND) in updater.out.getvalue()


def test_a_failed_find_links_install_prints_the_whole_command(
    updater, tmp_path, monkeypatch
):
    _receipt(tmp_path, monkeypatch, INDEX_RECEIPT)
    updater.run.install_code = 1
    selfupdate.on_response("GET", STALE)
    assert "Run by hand: " + " ".join(INDEX_COMMAND) in updater.out.getvalue()
    assert updater.execs == []


@pytest.mark.parametrize(
    "text",
    [
        # a registry install with no index recorded: a bare name would ask PyPI
        '[tool]\nrequirements = [{ name = "ream" }]\n',
        '[tool]\nrequirements = [{ name = "ream" }]\n\n[tool.options]\n',
        # an index that is not the only one: PyPI could still answer for `ream`
        _index_receipt(no_index=None),
        _index_receipt(no_index="false"),
        _index_receipt(no_index='"yes"'),
        # no index at all
        _index_receipt(links=None),
        _index_receipt(links="[]"),
        # an address that is not a plain web address: a directory, a file, plain http to a
        # remote host, a login in the address, a query, another scheme, a traversal, a
        # second argument smuggled in, a value that is not a list of text
        _index_receipt(links='["/srv/wheels"]'),
        _index_receipt(links='["file:///srv/wheels/"]'),
        _index_receipt(links='["http://artifacts.example/api/v1/tools/"]'),
        _index_receipt(links='["https://u:p@artifacts.example/tools/"]'),
        _index_receipt(links='["https://artifacts.example/tools/?t=x"]'),
        _index_receipt(links='["https://artifacts.example/tools/#x"]'),
        _index_receipt(links='["https://artifacts.example/../x/"]'),
        _index_receipt(links='["https://a.example/t/ --index-url x"]'),
        _index_receipt(links=f'["{INDEX}", 5]'),
        _index_receipt(links=f'"{INDEX}"'),
        # a requirement that is not the package, or that has a source of its own
        _index_receipt('{ name = "other" }'),
        _index_receipt('{ name = "ream", directory = "/home/x/artifacts" }'),
        _index_receipt('{ name = "ream", url = "https://x.example/ream.whl" }'),
        _index_receipt('{ name = "ream", path = "/x/ream.whl" }'),
    ],
)
def test_a_receipt_with_neither_a_git_address_nor_a_find_links_index_installs_nothing(
    updater, tmp_path, monkeypatch, text
):
    """Never a bare package name: with no source of its own the tool prints the manual line
    (the onboarding document's install line) and carries on."""
    _receipt(tmp_path, monkeypatch, text)
    selfupdate.on_response("GET", STALE)
    assert updater.run.calls == []
    assert updater.execs == []
    out = updater.out.getvalue()
    assert "/api/v1/agents.txt" in out
    assert "uv tool install" not in out


def test_a_loopback_index_over_plain_http_is_allowed(updater, tmp_path, monkeypatch):
    """A fresh install serves http://localhost:8000: its tools update from there."""
    local = "http://localhost:8000/api/v1/tools/"
    _receipt(tmp_path, monkeypatch, _index_receipt(links=f'["{local}"]'))
    selfupdate.on_response("GET", STALE)
    assert updater.run.installs == [
        [*INDEX_COMMAND[:-3], "--find-links", local, "ream"]
    ]
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)


def test_a_git_requirement_still_wins_over_find_links_options(
    updater, tmp_path, monkeypatch
):
    _receipt(tmp_path, monkeypatch, _index_receipt(f'{{ name = "ream", {GIT} }}'))
    assert selfupdate.configured_source() == SOURCE
    selfupdate.on_response("GET", STALE)
    assert updater.run.installs == [MAIN_COMMAND]
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)


def test_the_setting_still_comes_before_the_receipts_index(
    updater, tmp_path, monkeypatch
):
    _receipt(tmp_path, monkeypatch, INDEX_RECEIPT)
    monkeypatch.setenv("REAM_INSTALL_SOURCE", "git+https://env.example/b")
    selfupdate.on_response("GET", STALE)
    assert updater.run.installs == [
        [
            "uv",
            "tool",
            "install",
            "--force",
            "--refresh",
            "git+https://env.example/b@main",
        ]
    ]
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)


def test_a_wrong_setting_is_not_replaced_by_the_receipts_index(
    updater, tmp_path, monkeypatch
):
    _receipt(tmp_path, monkeypatch, INDEX_RECEIPT)
    monkeypatch.setenv("REAM_INSTALL_SOURCE", "https://evil.example/x.whl")
    selfupdate.on_response("GET", STALE)
    assert updater.run.calls == []
    assert updater.execs == []


def test_setting_then_file_then_receipt(updater, tmp_path, monkeypatch):
    _receipt(
        tmp_path, monkeypatch, f'[tool]\nrequirements = [{{ name = "ream", {GIT} }}]\n'
    )
    assert selfupdate.configured_source() == SOURCE  # receipt
    (tmp_path / "nocfg" / "ream").mkdir(parents=True)
    (tmp_path / "nocfg" / "ream" / "install-source").write_text(
        "git+https://file.example/a\n"
    )
    assert (
        selfupdate.configured_source() == "git+https://file.example/a"
    )  # file over receipt
    monkeypatch.setenv("REAM_INSTALL_SOURCE", "git+https://env.example/b")
    assert (
        selfupdate.configured_source() == "git+https://env.example/b"
    )  # env over both


def test_the_hint_carries_the_servers_note_and_installs_nothing(
    updater, tmp_path, monkeypatch
):
    _receipt(tmp_path, monkeypatch, None)
    note = "git+https://forge.example/runcove/artifacts"
    selfupdate.on_response(
        "GET",
        {**STALE, "x-ream-install-source": note},
        "https://forge.example/api/v1/x",
    )
    assert updater.run.calls == []
    assert updater.execs == []
    assert f"uv tool install --force {note}" in updater.out.getvalue()


def test_a_note_that_is_not_a_git_address_is_not_even_printed(
    updater, tmp_path, monkeypatch
):
    _receipt(tmp_path, monkeypatch, None)
    selfupdate.on_response(
        "GET", {**STALE, "x-ream-install-source": "https://evil.example/x.sh; rm -rf /"}
    )
    assert updater.run.calls == []
    assert "evil.example" not in updater.out.getvalue()


def test_a_note_on_another_host_is_not_printed_as_a_command(
    updater, tmp_path, monkeypatch
):
    _receipt(tmp_path, monkeypatch, None)
    note = "git+https://forge.example/runcove/artifacts"
    for api in ("https://other.example/api/v1/me", None):
        monkeypatch.setattr(selfupdate, "_done", False)
        selfupdate.on_response("GET", {**STALE, "x-ream-install-source": note}, api)
    text = updater.out.getvalue()
    assert "forge.example" not in text
    assert "uv tool install" not in text
    assert "/api/v1/agents.txt" in text


# ---- a command that changed something, or read stdin, is never run twice -----------


def test_a_post_before_the_out_of_date_answer_means_no_re_exec(updater):
    selfupdate.note_mutation()  # what _exchange does as a POST goes out
    selfupdate.on_response("GET", STALE)  # the stale answer arrives on a later GET
    assert len(updater.run.installs) == 1, "it still installs"
    assert updater.execs == []
    assert "updated; your next command uses the new tool" in updater.out.getvalue()


def test_the_exchange_and_connect_flag_a_post(monkeypatch):
    monkeypatch.setattr(selfupdate, "_mutated", False)
    monkeypatch.setattr(selfupdate, "on_response", lambda *a: None)

    class _Opener:
        def open(self, request, timeout=None):
            raise OSError("stop")

    monkeypatch.setattr(client, "_opener", _Opener())
    with pytest.raises(client.HTTPFailure):
        client._exchange(
            client._raw_request("GET", "https://example.invalid/x", "k"), 1, None
        )
    assert selfupdate._mutated is False
    with pytest.raises(client.HTTPFailure):
        client._exchange(
            client._raw_request("POST", "https://example.invalid/x", "k", b"{}"),
            1,
            None,
        )
    assert selfupdate._mutated is True


def test_connect_post_flags_and_feeds_the_check(monkeypatch):
    from ream import connect_cli

    monkeypatch.setattr(selfupdate, "_mutated", False)
    seen = []
    monkeypatch.setattr(selfupdate, "on_response", lambda m, h, u=None: seen.append(m))

    class _R:
        headers = {"X-Ream-Client-Outdated": "true"}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    class _Opener:
        def open(self, request, timeout=None):
            return _R()

    monkeypatch.setattr(client, "_opener", _Opener())
    connect_cli._post("https://example.invalid/api/v1/connect", {})
    assert selfupdate._mutated is True
    assert seen == ["POST"]


def test_reading_stdin_means_no_re_exec(updater, monkeypatch):
    import io

    from ream import decide

    monkeypatch.setattr(decide.sys, "stdin", io.StringIO("hello"))
    assert decide._read_text_arg("-") == "hello"
    assert decide._read_text_arg("plain") == "plain"
    selfupdate.on_response("GET", STALE)
    assert len(updater.run.installs) == 1
    assert updater.execs == []


# ---- the atomic stamp --------------------------------------------------------------


def test_the_stamp_is_claimed_exclusively(updater):
    assert selfupdate._attempt_allowed() is True
    assert selfupdate._attempt_allowed() is False
    stamp = selfupdate._stamp_path()
    assert (stamp.stat().st_mode & 0o777) == 0o600


def test_a_symlink_at_the_stamp_path_is_refused(updater, tmp_path):
    stamp = selfupdate._stamp_path()
    stamp.parent.mkdir(parents=True)
    target = tmp_path / "victim"
    target.write_text("keep")
    stamp.symlink_to(target)
    assert selfupdate._attempt_allowed() is False
    assert target.read_text() == "keep"
    assert stamp.is_symlink(), "the link is left alone, not followed or replaced"
    old = time.time() - selfupdate._MIN_GAP - 5
    os.utime(stamp, (old, old), follow_symlinks=False)
    assert selfupdate._attempt_allowed() is False, "even an old-looking link"


def test_a_stale_stamp_is_replaced_once(updater):
    assert selfupdate._attempt_allowed() is True
    stamp = selfupdate._stamp_path()
    old = time.time() - selfupdate._MIN_GAP - 5
    os.utime(stamp, (old, old))
    assert selfupdate._attempt_allowed() is True
    assert selfupdate._attempt_allowed() is False


# ---- the install's environment, the tool directory, argv[0] ------------------------


def test_the_install_environment_has_no_key_or_ream_variables(updater, monkeypatch):
    for name, value in {
        "REAM_KEY": "a",
        "ARTIFACTS_API": "b",
        "MY_API_TOKEN": "c",
        "SOME_SECRET": "d",
        "DB_PASSWORD": "e",
        "UV_PUBLISH_TOKEN": "f",
        "UV_INDEX_URL": "https://index.example/simple",
        "XDG_CONFIG_HOME": "/cfg",
        "SSL_CERT_FILE": "/ca.pem",
        "LANG": "C",
        "UNRELATED": "g",
    }.items():
        monkeypatch.setenv(name, value)
    selfupdate.on_response("GET", STALE)
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)
    env = updater.run.calls[0][1]["env"]
    assert {
        "PATH",
        "HOME",
        "UV_INDEX_URL",
        "XDG_CONFIG_HOME",
        "SSL_CERT_FILE",
        "LANG",
    } <= set(env)
    for dropped in (
        "REAM_KEY",
        "ARTIFACTS_API",
        "MY_API_TOKEN",
        "SOME_SECRET",
        "DB_PASSWORD",
        "UV_PUBLISH_TOKEN",
        "UNRELATED",
    ):
        assert dropped not in env
    assert all("env" in kw for _, kw in updater.run.calls)


def test_the_tool_directory_name_comes_from_where_the_tool_lives(
    updater, tmp_path, monkeypatch
):
    env = tmp_path / "tools" / "artifacts"
    env.mkdir(parents=True)
    monkeypatch.setattr(selfupdate.sys, "prefix", str(env))
    assert selfupdate._installed_level() == clientlevel.CLIENT_LEVEL
    python = [c[0] for c, _ in updater.run.calls if c[0].endswith("/python")]
    assert python == ["/tools/artifacts/bin/python"]


def test_a_bare_argv0_is_looked_up_on_path_never_in_the_working_directory(
    updater, monkeypatch
):
    monkeypatch.setattr(selfupdate, "_ORIGINAL_ARGV", ["ream", "list"])
    selfupdate.on_response("GET", STALE)
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)
    assert updater.execs == [("/bin/ream", ["ream", "list"])]


def test_a_relative_or_unfindable_argv0_is_not_executed(updater, monkeypatch):
    for argv0 in ("./ream", "sub/ream"):
        selfupdate._stamp_path().unlink(missing_ok=True)
        monkeypatch.setattr(selfupdate, "_done", False)
        monkeypatch.setattr(selfupdate, "_ORIGINAL_ARGV", [argv0, "list"])
        selfupdate.on_response("GET", STALE)
    monkeypatch.setattr(selfupdate, "_done", False)
    monkeypatch.setattr(selfupdate, "_ORIGINAL_ARGV", ["ream", "list"])
    selfupdate._stamp_path().unlink(missing_ok=True)
    monkeypatch.setattr(
        selfupdate.shutil, "which", lambda n: None if n == "ream" else f"/bin/{n}"
    )
    selfupdate.on_response("GET", STALE)
    assert len(updater.run.installs) == 3, "each really installed"
    assert updater.execs == []
    assert selfupdate._GUARD not in selfupdate.os.environ


# ---- never wait on a prompt; say plainly when the source needs a login --------------


def test_the_install_never_waits_on_a_prompt(updater):
    selfupdate.on_response("GET", STALE)
    install = [kw for c, kw in updater.run.calls if c[:3] == ["uv", "tool", "install"]]
    assert install[0]["stdin"] is selfupdate.subprocess.DEVNULL
    assert install[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    selfupdate.os.environ.pop("REAM_SELF_UPDATED", None)


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: could not read Username for 'https://forge.example': terminal prompts disabled",
        "remote: Authentication failed for 'https://forge.example/runcove/artifacts'",
        "error: HTTP status client error (401 Unauthorized) for url",
        "fatal: terminal prompts disabled",
    ],
)
def test_an_authentication_failure_says_the_source_needs_a_login(updater, stderr):
    updater.run.install_code = 1
    updater.run.install_stderr = stderr + " token=SEKRIT-CRED"
    selfupdate.on_response("GET", STALE)
    text = updater.out.getvalue()
    assert "needs a login on this machine" in text
    assert SOURCE in text
    assert " ".join(MAIN_COMMAND) in text
    assert "SEKRIT" not in text and "Username" not in text
    assert updater.execs == []


def test_another_failure_is_not_called_a_login_problem(updater):
    updater.run.install_code = 1
    updater.run.install_stderr = "error: no space left on device"
    selfupdate.on_response("GET", STALE)
    assert "login" not in updater.out.getvalue()
    assert "Run by hand" in updater.out.getvalue()


def test_a_login_failure_is_not_retried_within_the_hour(updater, monkeypatch):
    updater.run.install_code = 1
    updater.run.install_stderr = "fatal: could not read Username"
    selfupdate.on_response("GET", STALE)
    monkeypatch.setattr(selfupdate, "_done", False)
    selfupdate.on_response("GET", STALE)
    assert len(updater.run.installs) == 1


# ---- what is installed ----------------------------------------------------------------


def test_installed_commit_comes_from_the_receipt(tmp_path, monkeypatch):
    sha = "66eca1f0123456789abcdef0123456789abcdef0"
    _receipt(
        tmp_path,
        monkeypatch,
        '[tool]\nrequirements = [{ name = "ream", '
        f'git = "https://forge.example/runcove/artifacts?rev={sha}" }}]\n',
    )
    assert selfupdate.installed_commit() == sha
    _receipt(tmp_path, monkeypatch, None)
    (tmp_path / "toolenv" / "uv-receipt.toml").unlink(missing_ok=True)
    assert selfupdate.installed_commit() is None
    _receipt(
        tmp_path,
        monkeypatch,
        '[tool]\nrequirements = [{ name = "ream", directory = "/x" }]\n',
    )
    assert selfupdate.installed_commit() is None


@pytest.mark.parametrize("argv", [["--version"], ["version"]])
def test_ream_version_prints_version_level_and_commit(argv, monkeypatch, capsys):
    from ream import cli

    monkeypatch.setattr(selfupdate, "installed_commit", lambda: "abc1234def")
    monkeypatch.setattr(cli.plugin_cli, "load_or_exit", lambda: None)
    for name in ("REAM_KEY", "ARTIFACTS_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as stop:
        cli.main(argv)
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("ream ")
    assert f"level {clientlevel.CLIENT_LEVEL}" in out
    assert "commit abc1234def" in out


def test_ream_version_without_a_known_commit(monkeypatch, capsys):
    from ream import cli

    monkeypatch.setattr(selfupdate, "installed_commit", lambda: None)
    monkeypatch.setattr(cli.plugin_cli, "load_or_exit", lambda: None)
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "commit" not in capsys.readouterr().out
