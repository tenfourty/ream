"""`ream mcp-headers`: the one line of headers a Claude Code `headersHelper` reads.

The command prints `{"Authorization": "Bearer <key>", "X-Ream-Session": "<label>"}` and nothing
else on stdout, finds the key the way every other verb does, makes no request except the weekly
key swap of a key file over 7 days old, never updates the tool, and says what is wrong on stderr
without ever saying the key.

Every key here is a shape-valid DUMMY. A real key never appears in a test.
"""

import json
import os
import subprocess
import sys
import time

import pytest

from ream import cli, client, mcp_headers, plugin_cli, selfupdate

DUMMY = "art_" + "M" * 40
OTHER = "art_" + "N" * 40
FRESH = "art_" + "P" * 40

# The real exchange, taken before the autouse guard below replaces it.
_REAL_EXCHANGE = client._exchange


def _key_file(tmp_path, content: str = DUMMY, mode: int = 0o600, name: str = "key"):
    path = tmp_path / name
    path.write_text(content)
    path.chmod(mode)
    return path


def _fd_with(content: str) -> int:
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "w") as handle:
        handle.write(content)
    return read_fd


@pytest.fixture(autouse=True)
def _no_inherited_key(monkeypatch):
    """Neither a key file nor a descriptor may leak in from the shell running the suite."""
    for name in (
        "REAM_KEY_FILE",
        "ARTIFACTS_KEY_FILE",
        "REAM_KEY_FD",
        "ARTIFACTS_KEY_FD",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _self_update_state(monkeypatch):
    """`main` takes the process's one self-update decision; give it back after each test."""
    monkeypatch.setattr(selfupdate, "_done", False)


@pytest.fixture(autouse=True)
def _no_request(monkeypatch):
    """The helper runs on every connection of a session: a request here would be a bug."""

    def boom(*_a, **_k):
        raise AssertionError("mcp-headers made a request")

    monkeypatch.setattr(client._opener, "open", boom)
    monkeypatch.setattr(client, "_exchange", boom)


def test_a_key_file_gives_the_one_line_of_headers(tmp_path, capsys):
    path = _key_file(tmp_path)
    assert mcp_headers.main(["--key-file", str(path)]) == 0
    out, err = capsys.readouterr()
    assert out == json.dumps({"Authorization": f"Bearer {DUMMY}"}) + "\n"
    assert err == ""


def test_the_key_file_can_come_from_the_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REAM_KEY_FILE", str(_key_file(tmp_path)))
    assert mcp_headers.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"Authorization": f"Bearer {DUMMY}"}


def test_the_default_key_file_is_read_when_nothing_says_otherwise(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config" / "artifacts").mkdir(parents=True)
    _key_file(tmp_path / ".config" / "artifacts", name="key")
    assert mcp_headers.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"Authorization": f"Bearer {DUMMY}"}


def test_a_declared_session_rides_along(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REAM_SESSION", "artifacts-maintainer/5ed2e1c5")
    assert mcp_headers.main(["--key-file", str(_key_file(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "Authorization": f"Bearer {DUMMY}",
        "X-Ream-Session": "artifacts-maintainer/5ed2e1c5",
    }
    # One line, the key first, as Claude Code reads one JSON object.
    assert out.count("\n") == 1 and out.endswith("\n")
    assert list(json.loads(out)) == ["Authorization", "X-Ream-Session"]


def test_the_old_session_setting_name_works_and_the_flag_beats_both(
    tmp_path, monkeypatch, capsys
):
    path = _key_file(tmp_path)
    monkeypatch.setenv("ARTIFACTS_SESSION", "old/name")
    assert mcp_headers.main(["--key-file", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["X-Ream-Session"] == "old/name"
    assert mcp_headers.main(["--key-file", str(path), "--session", "flag/one"]) == 0
    assert json.loads(capsys.readouterr().out)["X-Ream-Session"] == "flag/one"


def test_no_declared_session_leaves_the_header_out(tmp_path, monkeypatch, capsys):
    # The agent tool's own markers do not make one up: the server labels the session itself.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "5ed2e1c5-0000")
    monkeypatch.setenv("QD_OFFICE", "artifacts")
    assert mcp_headers.main(["--key-file", str(_key_file(tmp_path))]) == 0
    assert "X-Ream-Session" not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("label", ["x" * 121, "tab\tin it", "bell\x07", "caf\u00e9"])
def test_a_session_label_the_server_would_refuse_stops_the_helper(
    tmp_path, capsys, label
):
    assert (
        mcp_headers.main(["--key-file", str(_key_file(tmp_path)), "--session", label])
        == 2
    )
    out, err = capsys.readouterr()
    assert out == ""
    assert "session label" in err and DUMMY not in err


def test_a_key_through_a_descriptor(capsys):
    fd = _fd_with(DUMMY + "\n")
    try:
        assert mcp_headers.main(["--key-fd", str(fd)]) == 0
    finally:
        os.close(fd)
    out, err = capsys.readouterr()
    assert json.loads(out) == {"Authorization": f"Bearer {DUMMY}"}
    assert err == ""


def test_a_key_through_the_wrappers_descriptor_variable(monkeypatch, capsys):
    """`artifacts-key -- ream mcp-headers` hands the key over as $REAM_KEY_FD."""
    fd = _fd_with(DUMMY)
    monkeypatch.setenv("REAM_KEY_FD", str(fd))
    try:
        assert mcp_headers.main([]) == 0
    finally:
        os.close(fd)
    assert json.loads(capsys.readouterr().out) == {"Authorization": f"Bearer {DUMMY}"}


def test_a_descriptor_and_a_file_together_are_refused_not_preferred(tmp_path, capsys):
    fd = _fd_with(OTHER)
    try:
        code = mcp_headers.main(
            ["--key-fd", str(fd), "--key-file", str(_key_file(tmp_path))]
        )
    finally:
        os.close(fd)
    out, err = capsys.readouterr()
    assert code == 2 and out == ""
    assert "two answers to the same question" in err
    assert DUMMY not in err and OTHER not in err


def test_a_missing_key_prints_nothing_to_stdout_and_exits_non_zero(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("REAM_KEY_FILE", str(tmp_path / "shredded"))
    assert mcp_headers.main([]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "cannot read key file" in err
    assert "Bearer" not in err


def test_a_key_file_others_can_read_is_refused_without_showing_the_key(
    tmp_path, capsys
):
    path = _key_file(tmp_path, mode=0o644)
    assert mcp_headers.main(["--key-file", str(path)]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "chmod 600" in err and DUMMY not in err


def test_a_file_that_is_not_a_key_is_refused_without_showing_it(tmp_path, capsys):
    secretish = "art_short and not a key"
    path = _key_file(tmp_path, content=secretish)
    assert mcp_headers.main(["--key-file", str(path)]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "not a valid agent key" in err and secretish not in err


def test_a_bad_descriptor_is_a_usage_error_not_a_crash(capsys):
    import resource

    closed, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert mcp_headers.main(["--key-fd", str(closed)]) == 2
    out, err = capsys.readouterr()
    assert out == "" and f"cannot read --key-fd {closed}" in err


def test_an_unexpected_fault_names_its_class_never_its_text(
    tmp_path, monkeypatch, capsys
):
    """An exception's text can carry the value being handled (http.client's "Invalid header
    value" embeds the whole header); only the class name is printed."""

    def explode(_args):
        raise ValueError(f"Bearer {DUMMY}")

    monkeypatch.setattr(mcp_headers, "headers", explode)
    assert mcp_headers.main(["--key-file", str(_key_file(tmp_path))]) == 1
    out, err = capsys.readouterr()
    assert out == "" and err == "error: ValueError\n"


def test_it_writes_nothing_and_leaves_the_key_file_as_it_was(
    tmp_path, monkeypatch, capsys
):
    """No key copied anywhere, no news state file, no rotation: reading only."""
    path = _key_file(tmp_path)

    def state():
        info = path.stat()  # not the access time: reading is allowed to move that
        return (
            sorted(p.name for p in tmp_path.iterdir()),
            (info.st_mode, info.st_size, info.st_mtime_ns),
        )

    before = state()
    assert mcp_headers.main(["--key-file", str(path)]) == 0
    capsys.readouterr()
    assert state() == before
    assert path.read_text() == DUMMY
    assert not (tmp_path / "xdg-state").exists()


def _age(path, days: float) -> None:
    then = time.time() - days * 24 * 3600
    os.utime(path, (then, then))


@pytest.fixture()
def service(monkeypatch):
    """A stand-in for the service's `/me` and `/keys/rotate`, recording every call. The
    helper's own request guard (`_no_request`) stays in force below `client._request`."""
    calls: list[tuple[str, str, str]] = []
    state = {"rotating": True, "fail": False}

    def fake(method, url, key, body, *_a, **_k):
        calls.append((method, url.rsplit("/", 1)[-1], key))
        if state["fail"]:
            raise client.HTTPFailure(503, "down")
        if method == "GET":
            return {"rotating": state["rotating"]}
        return {"key": FRESH}

    monkeypatch.setattr(client, "_request", fake)
    return calls, state


def test_a_due_rotating_key_file_is_swapped_and_the_new_key_is_sent(
    tmp_path, service, capsys
):
    calls, _state = service
    path = _key_file(tmp_path)
    _age(path, 8)
    assert mcp_headers.main(["--key-file", str(path)]) == 0
    out, err = capsys.readouterr()
    assert path.read_text().strip() == FRESH
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert [c[:2] for c in calls] == [("GET", "me"), ("POST", "rotate")]
    assert out == json.dumps({"Authorization": f"Bearer {FRESH}"}) + "\n"
    assert json.loads(out.splitlines()[0]) == {"Authorization": f"Bearer {FRESH}"}
    assert "key was replaced" in err
    assert DUMMY not in out + err and FRESH not in err


def test_an_old_key_file_that_is_not_rotating_is_left_alone(tmp_path, service, capsys):
    calls, state = service
    state["rotating"] = False
    path = _key_file(tmp_path)
    _age(path, 8)
    assert mcp_headers.main(["--key-file", str(path)]) == 0
    out, err = capsys.readouterr()
    assert [c[:2] for c in calls] == [("GET", "me")]
    assert path.read_text() == DUMMY
    assert json.loads(out) == {"Authorization": f"Bearer {DUMMY}"} and err == ""


def test_a_fresh_key_file_makes_no_network_call(tmp_path, service, capsys):
    calls, _state = service
    path = _key_file(tmp_path)
    _age(path, 6)
    assert mcp_headers.main(["--key-file", str(path)]) == 0
    assert calls == []
    assert json.loads(capsys.readouterr().out) == {"Authorization": f"Bearer {DUMMY}"}
    assert path.read_text() == DUMMY


def test_a_key_from_a_descriptor_is_never_rotated(
    tmp_path, service, monkeypatch, capsys
):
    calls, _state = service
    # An old file is lying about: a descriptor key must not make it the one examined.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config" / "artifacts").mkdir(parents=True)
    old = _key_file(tmp_path / ".config" / "artifacts", content=OTHER, name="key")
    _age(old, 30)
    fd = _fd_with(DUMMY)
    try:
        assert mcp_headers.main(["--key-fd", str(fd)]) == 0
    finally:
        os.close(fd)
    assert calls == []
    assert json.loads(capsys.readouterr().out) == {"Authorization": f"Bearer {DUMMY}"}
    assert old.read_text() == OTHER

    fd = _fd_with(DUMMY)
    monkeypatch.setenv("REAM_KEY_FD", str(fd))
    try:
        assert mcp_headers.main([]) == 0
    finally:
        os.close(fd)
    assert calls == []
    assert json.loads(capsys.readouterr().out) == {"Authorization": f"Bearer {DUMMY}"}
    assert old.read_text() == OTHER


def test_a_failed_swap_still_prints_the_old_key_and_exits_zero(
    tmp_path, service, capsys
):
    calls, state = service
    state["fail"] = True
    path = _key_file(tmp_path)
    _age(path, 8)
    assert mcp_headers.main(["--key-file", str(path)]) == 0
    out, err = capsys.readouterr()
    assert calls, "the swap was tried"
    assert out == json.dumps({"Authorization": f"Bearer {DUMMY}"}) + "\n"
    assert "weekly key swap did not happen" in err
    assert DUMMY not in err and path.read_text() == DUMMY


def test_the_dispatcher_runs_it_as_a_verb(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ream"])  # `cli.main` rewrites it: restored after
    path = _key_file(tmp_path)
    with pytest.raises(SystemExit) as stop:
        cli.main(["mcp-headers", "--key-file", str(path)])
    assert stop.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"Authorization": f"Bearer {DUMMY}"}


def test_the_global_flags_may_come_before_the_verb(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ream"])
    path = _key_file(tmp_path)
    with pytest.raises(SystemExit) as stop:
        cli.main(["--session", "a/b", "--key-file", str(path), "mcp-headers"])
    assert stop.value.code == 0
    assert json.loads(capsys.readouterr().out) == {
        "Authorization": f"Bearer {DUMMY}",
        "X-Ream-Session": "a/b",
    }


def test_usage_names_the_command_as_it_was_run(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ream"])
    with pytest.raises(SystemExit):
        cli.main(["mcp-headers", "--help"])
    assert capsys.readouterr().out.startswith("usage: ream mcp-headers")


def test_the_verb_is_listed_and_no_plug_in_may_take_it():
    assert "mcp-headers" in cli._help()
    assert "mcp-headers" in plugin_cli.reserved_verbs()


def test_the_real_command_prints_one_line_and_nothing_else(tmp_path):
    """End to end, in a fresh interpreter: the plug-in loader, the news and the self-update
    check must not put a byte on stdout (or anything but a note on stderr)."""
    path = _key_file(tmp_path)
    code = (
        "import sys; sys.argv = ['ream', 'mcp-headers', '--key-file', sys.argv[1]];"
        "from ream import cli; cli.main()"
    )
    env = {
        **os.environ,
        "REAM_SESSION": "artifacts-maintainer/mcp",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    done = subprocess.run(  # noqa: S603 -- our own interpreter and a fixed script
        [sys.executable, "-I", "-c", code, str(path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == (
        json.dumps(
            {
                "Authorization": f"Bearer {DUMMY}",
                "X-Ream-Session": "artifacts-maintainer/mcp",
            }
        )
        + "\n"
    )
    assert DUMMY not in done.stderr


def test_a_failure_to_find_the_api_address_returns_the_old_key(
    tmp_path, service, monkeypatch, capsys
):
    calls, _state = service

    def broken():
        raise client.UsageError("no api address")

    monkeypatch.setattr(client, "_api_base", broken)
    path = _key_file(tmp_path)
    _age(path, 8)
    assert mcp_headers.main(["--key-file", str(path)]) == 0
    out, err = capsys.readouterr()
    assert calls == []
    assert out == json.dumps({"Authorization": f"Bearer {DUMMY}"}) + "\n"
    assert "weekly key swap did not happen" in err
    assert DUMMY not in err and path.read_text() == DUMMY


class _OutdatedResponse:
    """What the service sends to an out-of-date tool, whatever the path."""

    def __init__(self, body: bytes):
        self._body = body
        self.headers = {"X-Ream-Client-Outdated": "true"}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


def _outdated_service(monkeypatch, calls):
    """The real `client._exchange` over a fake connection whose every answer says the tool is
    out of date, with an install source set so `selfupdate.on_response` would act on it, and
    the updater's entry points rigged to fail the test if reached. Returns what they saw."""

    def opener(request, timeout=None):
        calls.append((request.get_method(), request.full_url.rsplit("/", 1)[-1]))
        if request.get_method() == "GET":
            return _OutdatedResponse(b'{"rotating": true}')
        return _OutdatedResponse(json.dumps({"key": FRESH}).encode())

    updater_calls: list[str] = []

    def updater(name):
        def called(*_a, **_k):
            updater_calls.append(name)
            raise AssertionError(f"the helper ran the updater ({name})")

        return called

    monkeypatch.setattr(client, "_exchange", _REAL_EXCHANGE)
    monkeypatch.setattr(client._opener, "open", opener)
    monkeypatch.setenv("REAM_INSTALL_SOURCE", "git+https://example.invalid/ream")
    for name in (
        "REAM_NO_SELF_UPDATE",
        "ARTIFACTS_NO_SELF_UPDATE",
        "REAM_SELF_UPDATED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(selfupdate.shutil, "which", lambda *_a, **_k: "/bin/uv")
    monkeypatch.setattr(selfupdate, "_attempt_allowed", lambda: True)
    monkeypatch.setattr(selfupdate, "_run", updater("_run"))
    monkeypatch.setattr(selfupdate.os, "execv", updater("os.execv"))
    return updater_calls


def test_an_outdated_answer_during_the_key_swap_never_runs_the_updater(
    tmp_path, monkeypatch, capsys
):
    calls: list[tuple[str, str]] = []
    updater_calls = _outdated_service(monkeypatch, calls)
    path = _key_file(tmp_path)
    _age(path, 8)
    assert mcp_headers.main(["--key-file", str(path)]) == 0
    out, err = capsys.readouterr()
    assert calls == [("GET", "me"), ("POST", "rotate")], "the swap itself still happens"
    assert updater_calls == []
    assert out == json.dumps({"Authorization": f"Bearer {FRESH}"}) + "\n"
    assert "out of date" not in err


def test_the_rig_does_reach_the_updater_for_a_command_that_may_update(monkeypatch):
    """The control for the test above: without the helper's switch the same answers do run
    the updater, so that test's silence is the switch and not a rig that cannot fire."""
    updater_calls = _outdated_service(monkeypatch, [])
    with pytest.raises(AssertionError, match="ran the updater"):
        client._request("GET", "https://ream.invalid/api/v1/me", DUMMY, None)
    assert updater_calls == ["_run"]
