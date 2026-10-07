"""`artifacts-admin new-key` needs no database, so it must run where only the
client is installed (the uv tool on baba has no psycopg). It used to die on
import: admin.py pulled in the database modules at the top (homelab
maintainer, 7 Oct 2026, minting retrobrew_maintainer)."""

import subprocess
import sys

BLOCK_PSYCOPG = (
    "import sys; sys.modules['psycopg'] = None; "
    "from artifacts import admin; admin.main(sys.argv[1:])"
)


def test_new_key_runs_without_psycopg(tmp_path):
    out = tmp_path / "k" / "key"
    run = subprocess.run(  # noqa: S603 -- our own interpreter and a fixed script
        [
            sys.executable,
            "-c",
            BLOCK_PSYCOPG,
            "new-key",
            "--agent",
            "probe-seat",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert "sha256=" in run.stdout
    assert out.read_text().startswith("art_")
    # The token itself never reaches stdout.
    assert out.read_text() not in run.stdout


def test_a_database_command_without_psycopg_fails_saying_so(tmp_path):
    run = subprocess.run(  # noqa: S603 -- our own interpreter and a fixed script
        [sys.executable, "-c", BLOCK_PSYCOPG, "list-keys"],
        capture_output=True,
        text=True,
        check=False,
        env={"DATABASE_URL": "postgresql://nowhere/x", "PATH": ""},
    )
    assert run.returncode != 0
