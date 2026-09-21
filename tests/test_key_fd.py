"""A key from an open descriptor, so it never touches disk.

homelab-ij8.34. Four bearer tokens sat in cleartext in ~/.config/artifacts/,
and mode 600 is real protection against another user and none at all against a
stray read. The vaulted key lives in sops and a wrapper hands one entry
straight to the tool:

    artifacts-publish --key-fd 3 page.html 3< <(sops -d --extract ... FILE)

A descriptor rather than the temporary file I first proposed -- the operator's
improvement, 21 Sep 2026. With no file there is nothing to shred, no window in
which the plaintext is on disk, and no trap that has to fire to clean up.

Every token here is a shape-valid DUMMY. A real key never appears in a test,
a fixture or a transcript.
"""

import os

import pytest

from artifacts import client, decide

DUMMY = "art_" + "D" * 40


def _fd_with(content: str) -> int:
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "w") as handle:
        handle.write(content)
    return read_fd


def test_a_key_arrives_through_a_descriptor(tmp_path):
    fd = _fd_with(DUMMY)
    try:
        assert client._read_key_fd(fd) == DUMMY
    finally:
        os.close(fd)


def test_one_trailing_newline_is_allowed_exactly_as_for_a_file():
    """A wrapper that pipes `sops -d` gets a trailing newline from the shell
    more often than not, and refusing it would make the documented idiom fail
    for a reason nobody would guess."""
    fd = _fd_with(DUMMY + "\n")
    try:
        assert client._read_key_fd(fd) == DUMMY
    finally:
        os.close(fd)


def test_a_carriage_return_is_still_refused():
    """`newline=""` keeps the "\\r" so the shape check can see it, rather than
    a universal-newline read quietly turning "\\r\\n" into "\\n" first -- the
    same reasoning the file reader carries."""
    fd = _fd_with(DUMMY + "\r\n")
    try:
        with pytest.raises(client.UsageError):
            client._read_key_fd(fd)
    finally:
        os.close(fd)


def test_a_truncated_read_is_refused_and_never_shown():
    """A partial read and a wrong key look identical from here, and saying
    which would mean printing it. The character COUNT is safe and is the one
    fact that helps."""
    fd = _fd_with("art_TRUNC")
    try:
        with pytest.raises(client.UsageError) as raised:
            client._read_key_fd(fd)
    finally:
        os.close(fd)
    message = str(raised.value)
    assert "art_TRUNC" not in message
    assert "9 characters read" in message


def test_the_processs_own_output_streams_are_refused():
    """Reading a key from stdout or stderr is always a mistake and a silent
    one: it would block or read nothing, and the shape check would then blame
    the key."""
    for number in (1, 2):
        with pytest.raises(client.UsageError) as raised:
            client._read_key_fd(number)
        assert "output stream" in str(raised.value)


def test_a_descriptor_nobody_opened_says_what_to_do():
    with pytest.raises(client.UsageError) as raised:
        client._read_key_fd(19)
    message = str(raised.value)
    assert "cannot read --key-fd 19" in message
    assert "3< <(" in message, "the error carries the idiom, not just the fault"


def test_the_caller_keeps_its_own_descriptor_open():
    """closefd=False: the descriptor belongs to whoever opened it, and a tool
    that closed it out from under a wrapper would break the next command in
    the pipeline for reasons nobody would trace back here."""
    fd = _fd_with(DUMMY)
    try:
        client._read_key_fd(fd)
        os.fstat(fd)  # raises if it was closed
    finally:
        os.close(fd)


# --- the two flags must not quietly disagree ----------------------------------


def test_both_flags_at_once_is_refused_rather_than_one_being_preferred(tmp_path):
    """The failure this prevents is specific: a wrapper that sets both ends up
    reading the cleartext file it was written to stop using, and everything
    appears to work."""
    key_file = tmp_path / "key"
    key_file.write_text(DUMMY)
    key_file.chmod(0o600)
    args = client.build_parser().parse_args(
        ["--key-fd", "3", "--key-file", str(key_file), "--list"]
    )
    with pytest.raises(client.UsageError) as raised:
        client._resolve_key(args)
    assert "two answers to the same question" in str(raised.value)


def test_with_neither_flag_it_still_reads_the_default_file(tmp_path, monkeypatch):
    key_file = tmp_path / "key"
    key_file.write_text(DUMMY)
    key_file.chmod(0o600)
    monkeypatch.setenv("ARTIFACTS_KEY_FILE", str(key_file))
    args = client.build_parser().parse_args(["--list"])
    assert client._resolve_key(args) == DUMMY


def test_the_decisions_tool_takes_it_too(tmp_path):
    """Or the wrapper covers publishing and not asking, which is half of what
    an agent does here."""
    args = decide.build_parser().parse_args(["--key-fd", "3", "list"])
    assert args.key_fd == 3


def test_a_bad_key_fd_exits_as_a_usage_error_not_a_crash(capsys):
    assert client.main(["--key-fd", "19", "--list"]) == 2
    assert "cannot read --key-fd 19" in capsys.readouterr().err


def test_a_missing_key_file_says_where_a_key_comes_from_now(tmp_path, monkeypatch, capsys):
    """The cleartext key files are being shredded seat by seat as they move
    into the vault (homelab-ij8.34). From here on, the commonest reason this
    file is missing is that it is gone ON PURPOSE -- and "no such file" alone
    sends the reader hunting for a lost key, or minting a second one for an
    identity that already exists."""
    monkeypatch.setenv("ARTIFACTS_KEY_FILE", str(tmp_path / "shredded"))
    monkeypatch.delenv("ARTIFACTS_KEY_FD", raising=False)
    assert client.main(["--list"]) == 2
    err = capsys.readouterr().err
    assert "artifacts-key" in err, "it names the wrapper"
    assert "baba_operator" in err, "and the seats it takes"
    assert "--key-fd" in err, "and what to do somewhere that has no vault"
