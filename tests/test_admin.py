"""Key tooling: `artifacts-admin new-key` never needs a database (the token is
generated where it will be used) and never lets the token reach stdout,
stderr, or an existing file; `register-key` only ever sees the token's
SHA-256, and `list-keys` never prints one.
"""

import hashlib
import os
import re
import stat

import pytest

from artifacts import keys
from artifacts.admin import main

AGENT = "ops-seat"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_new_key_needs_no_database(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    main(["new-key", "--agent", AGENT, "--out", str(tmp_path / "key")])
    assert (tmp_path / "key").exists()


def test_new_key_file_and_parent_modes(tmp_path):
    out = tmp_path / "fresh-subdir" / "key"
    main(["new-key", "--agent", AGENT, "--out", str(out)])
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(out.parent).st_mode) == 0o700


def test_new_key_token_matches_mint_format_with_no_trailing_newline(tmp_path):
    out = tmp_path / "key"
    main(["new-key", "--agent", AGENT, "--out", str(out)])
    content = out.read_bytes()
    assert content.decode().startswith("art_")
    assert not content.endswith(b"\n")


def test_new_key_refuses_an_existing_path_and_leaves_it_untouched(tmp_path):
    out = tmp_path / "key"
    out.write_bytes(b"sentinel")
    with pytest.raises(SystemExit):
        main(["new-key", "--agent", AGENT, "--out", str(out)])
    assert out.read_bytes() == b"sentinel"


def test_new_key_token_appears_in_neither_stdout_nor_stderr(tmp_path, capsys):
    out = tmp_path / "key"
    main(["new-key", "--agent", AGENT, "--out", str(out)])
    token = out.read_text()
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err


def test_new_key_prints_agent_path_sha256_and_register_command(tmp_path, capsys):
    out = tmp_path / "key"
    main(["new-key", "--agent", AGENT, "--out", str(out)])
    token = out.read_text()
    digest = _sha256(token.encode())
    captured = capsys.readouterr()
    assert AGENT in captured.out
    assert str(out) in captured.out
    assert f"sha256={digest}" in captured.out
    assert (
        "kubectl -n artifacts exec deploy/artifacts -- artifacts-admin "
        f"register-key --agent {AGENT} --sha256 {digest}" in captured.out
    )


@pytest.mark.parametrize("bad_agent", ["", "Foo", "-abc", "a b", "a_b", "a" * 64])
def test_new_key_rejects_bad_agent_names(tmp_path, bad_agent):
    with pytest.raises(SystemExit):
        main(["new-key", "--agent", bad_agent, "--out", str(tmp_path / "key")])
    assert not (tmp_path / "key").exists()


def test_register_key_inserts_and_verify_finds_the_agent(db, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    out = tmp_path / "key"
    main(["new-key", "--agent", AGENT, "--out", str(out)])
    token = out.read_text()
    digest = _sha256(token.encode())
    main(["register-key", "--agent", AGENT, "--sha256", digest])
    verified = keys.verify(db, token)
    assert verified is not None and verified.agent == AGENT


def test_register_key_prints_key_id_and_agent(db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    digest = "a" * 64
    main(["register-key", "--agent", AGENT, "--sha256", digest])
    out = capsys.readouterr().out
    assert "agent=" + AGENT in out
    assert "key_id=" in out


@pytest.mark.parametrize(
    "bad_hash",
    ["a" * 63, "A" * 64, "g" * 64, "not-hex-at-all-and-wrong-length-too-short"],
)
def test_register_key_rejects_bad_hashes_and_inserts_nothing(db, monkeypatch, bad_hash):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    with pytest.raises(SystemExit):
        main(["register-key", "--agent", AGENT, "--sha256", bad_hash])
    with db.connect() as conn:
        count = conn.execute("select count(*) from agent_key").fetchone()[0]
    assert count == 0


def test_register_key_rejects_bad_agent_and_inserts_nothing(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    with pytest.raises(SystemExit):
        main(["register-key", "--agent", "Not Valid", "--sha256", "a" * 64])
    with db.connect() as conn:
        count = conn.execute("select count(*) from agent_key").fetchone()[0]
    assert count == 0


def test_revoke_key_makes_verify_return_none(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    token, key_id = keys.mint(db, agent=AGENT)
    main(["revoke-key", key_id])
    assert keys.verify(db, token) is None


def test_revoke_key_prints_the_agent_it_revoked(db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    _token, key_id = keys.mint(db, agent=AGENT)
    main(["revoke-key", key_id])
    out = capsys.readouterr().out
    assert f"revoked key_id={key_id} agent={AGENT}" in out


def test_revoke_key_on_an_unknown_id_fails_loudly(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    with pytest.raises(SystemExit) as excinfo:
        main(["revoke-key", "no-such-id"])
    assert "no active key with id no-such-id" in str(excinfo.value)


def test_revoke_key_a_second_time_fails_loudly_instead_of_a_silent_no_op(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    _token, key_id = keys.mint(db, agent=AGENT)
    main(["revoke-key", key_id])
    with pytest.raises(SystemExit) as excinfo:
        main(["revoke-key", key_id])
    assert f"no active key with id {key_id}" in str(excinfo.value)


def test_list_keys_never_prints_a_hash(db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db.dsn)
    _token, key_id = keys.mint(db, agent=AGENT)
    main(["revoke-key", key_id])
    keys.mint(db, agent="second-seat")
    main(["list-keys"])
    out = capsys.readouterr().out
    assert AGENT in out
    assert "second-seat" in out
    assert key_id in out
    # No 64-char lowercase hex run anywhere in the output.
    assert re.search(r"[0-9a-f]{64}", out) is None
