"""`ream mcp-headers`: the one line of headers a Claude Code `headersHelper` reads.

The command prints `{"Authorization": "Bearer <key>", "X-Ream-Session": "<label>"}` and nothing
else on stdout, finds the key the way every other verb does, makes no request, and says what is
wrong on stderr without ever saying the key.

Every key here is a shape-valid DUMMY. A real key never appears in a test.
"""

import json
import os
import subprocess
import sys

import pytest

from ream import cli, client, mcp_headers, plugin_cli

DUMMY = "art_" + "M" * 40
OTHER = "art_" + "N" * 40


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
