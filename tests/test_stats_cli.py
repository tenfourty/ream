"""`ream stats PAGE-ID` (runcove-5t3oj.158.1, slice 3): the stats route from the command line,
against the real app over real HTTP (the `live` server of `tests/test_client.py`)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_client import key_file, live  # noqa: F401

from ream import cli, client, email_share, keys, pageviews, store

PAGE = "<!doctype html><html><head><title>B</title></head><body>one</body></html>"


@pytest.fixture()
def page(db, key_file):
    return store.publish(db, "ops-seat", "Board", PAGE).id


def run(path, *args) -> int:
    return client.stats_main(["--key-file", str(path), *args])


def test_a_page_nobody_read_says_so(live, key_file, page, capsys):
    path, _ = key_file
    assert run(path, page) == 0
    out = capsys.readouterr().out
    assert "no views counted yet" in out
    assert "first view counted on" not in out


def test_the_counts_print_per_kind_with_since_and_recipients(
    db, live, key_file, page, capsys
):
    path, _ = key_file
    now = datetime.now(UTC)
    pageviews.record(db, page, "owner", "", now=now - timedelta(days=1))
    pageviews.record(db, page, "owner", "", now=now)
    share, _t = email_share.create(db, page, "ann@example.org")
    pageviews.record(db, page, "external", share.id, now=now)
    assert run(path, page, "--days", "7") == 0
    out = capsys.readouterr().out
    day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    assert f"first view counted on {day}" in out
    assert "owner 2" in out and "external 1" in out and "agent 0" in out
    assert now.strftime("%Y-%m-%d") in out
    assert "ann@example.org" in out and "1 open" in out


def test_json_prints_the_routes_answer(db, live, key_file, page, capsys):
    path, _ = key_file
    pageviews.record(db, page, "owner", "", now=datetime.now(UTC))
    assert run(path, page, "--json", "--days", "2") == 0
    got = json.loads(capsys.readouterr().out)
    assert got["artifact_id"] == page and len(got["days"]) == 2
    assert got["totals"]["owner"] == 1


def test_control_characters_in_an_email_never_reach_the_terminal(
    db, live, key_file, page, capsys
):
    path, _ = key_file
    email_share.create(db, page, "a\x1b[31m@example.org")
    assert run(path, page) == 0
    assert "\x1b" not in capsys.readouterr().out


def test_another_agents_page_prints_the_refusal(live, db, page, tmp_path, capsys):
    token, _ = keys.mint(db, agent="other")
    other = tmp_path / "other-key"
    other.write_text(token)
    other.chmod(0o600)
    assert run(other, page) == 1
    assert capsys.readouterr().err.strip() == "404 no such page"


@pytest.mark.parametrize("days", ["0", "366", "x"])
def test_days_out_of_range_is_a_usage_error(tmp_path, days):
    with pytest.raises(SystemExit) as stop:
        client.stats_main(["page", "--days", days])
    assert stop.value.code == 2


def test_ream_stats_runs_it(live, key_file, page, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["ream"])  # `cli.main` rewrites it: restored after
    path, _ = key_file
    with pytest.raises(SystemExit) as stop:
        cli.main(["stats", page, "--key-file", str(path)])
    assert stop.value.code == 0
    assert "stats" in cli._help()
