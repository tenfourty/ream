"""Machine keys replace themselves weekly (homelab-ij8.34, Jeremy's (3a) of
24 Sep 2026): the old key dies when the new one is first used, a key never
swapped is refused after two weeks, and a thief who swaps first locks the real
machine out -- loudly."""

import contextlib
import fcntl
import multiprocessing
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_client import live  # noqa: F401

from ream import client, keys
from ream.app import create_gallery_app


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


def _rotating_key(db, agent="laptop"):
    token, key_id = keys.mint(db, agent=agent)
    with db.connect() as conn:
        conn.execute("update agent_key set rotating = true where id = %s", (key_id,))
    return token, key_id


def _me(api, token):
    return api.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})


def _rotate(api, token):
    return api.post("/api/v1/keys/rotate", headers={"Authorization": f"Bearer {token}"})


def test_a_connected_machines_key_rotates_and_a_house_key_does_not(db, api):
    rotating, _ = _rotating_key(db)
    house, _ = keys.mint(db, agent="house-seat")
    assert _me(api, rotating).json()["rotating"] is True
    assert _me(api, rotating).json()["rotate_after"]
    assert _me(api, house).json()["rotating"] is False
    assert _rotate(api, house).status_code == 409


def test_the_old_key_dies_when_the_new_one_is_first_used(db, api):
    old, _ = _rotating_key(db)
    swapped = _rotate(api, old)
    assert swapped.status_code == 200
    new = swapped.json()["key"]
    assert swapped.json()["agent"] == "laptop"
    assert _me(api, old).status_code == 200, "a lost response must not lock it out"
    assert _me(api, new).status_code == 200  # first use
    dead = _me(api, old)
    assert dead.status_code == 401
    assert "replaced by a newer one" in dead.json()["detail"]
    assert "tell Jeremy" in dead.json()["detail"]


def test_a_thief_who_swaps_first_locks_the_real_machine_out_loudly(db, api):
    real, _ = _rotating_key(db)
    stolen = real
    thiefs = _rotate(api, stolen).json()["key"]
    _me(api, thiefs)
    refused = _me(api, real)
    assert refused.status_code == 401
    assert "someone else has held its key" in refused.json()["detail"]


def test_only_one_pending_successor_at_a_time(db, api):
    old, _ = _rotating_key(db)
    first = _rotate(api, old).json()["key"]
    second = _rotate(api, old).json()["key"]
    assert _me(api, first).status_code == 401, (
        "an unused earlier successor is withdrawn"
    )
    assert _me(api, second).status_code == 200


def test_a_key_never_swapped_is_refused_after_two_weeks(db, api):
    token, key_id = _rotating_key(db)
    with db.connect() as conn:
        conn.execute(
            "update agent_key set created_at = %s where id = %s",
            (datetime.now(UTC) - timedelta(days=15), key_id),
        )
    refused = _me(api, token)
    assert refused.status_code == 401
    assert "expired" in refused.json()["detail"]
    assert "artifacts-connect" in refused.json()["detail"]


def test_the_tools_swap_a_week_old_key_file_without_showing_it(
    db, live, tmp_path, capsys
):
    token, _ = _rotating_key(db, agent="ops-seat")
    key_file = tmp_path / "key"
    key_file.write_text(token + "\n")
    key_file.chmod(0o600)
    week_ago = time.time() - 8 * 24 * 3600
    os.utime(key_file, (week_ago, week_ago))
    assert client.main(["--key-file", str(key_file), "--list"]) == 0
    new = key_file.read_text().strip()
    assert new != token and new.startswith("art_")
    assert oct(key_file.stat().st_mode & 0o777) == "0o600"
    out = capsys.readouterr()
    assert "key was replaced, as it is every week" in out.err
    assert new not in out.out + out.err and token not in out.out + out.err


def test_a_fresh_key_file_costs_no_extra_request(db, live, tmp_path, monkeypatch):
    token, _ = _rotating_key(db, agent="ops-seat")
    key_file = tmp_path / "key"
    key_file.write_text(token + "\n")
    key_file.chmod(0o600)
    calls = []
    real = client._request

    def counting(method, url, *a, **kw):
        calls.append(url)
        return real(method, url, *a, **kw)

    monkeypatch.setattr(client, "_request", counting)
    assert client.main(["--key-file", str(key_file), "--list"]) == 0
    assert not any(u.endswith(("/me", "/keys/rotate")) for u in calls)
    assert key_file.read_text().strip() == token


def test_the_machines_page_does_not_list_weekly_swaps_as_revocations(db, api):
    old, _ = _rotating_key(db)
    new = _rotate(api, old).json()["key"]
    _me(api, new)
    page = api.get("/machines").text
    assert page.count("<h2>laptop</h2>") == 1
    assert "renews itself weekly" in page
    assert "Revoked (" not in page


# The weekly swap is one at a time per key file (`<key file>.lock`): the service withdraws an unused
# pending successor, so two unserialised swaps could leave the file holding a revoked key.


def _stale_key_file(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("art_old_0123456789abcdefghij\n")
    key_file.chmod(0o600)
    week_ago = time.time() - 8 * 24 * 3600
    os.utime(key_file, (week_ago, week_ago))
    return key_file


def _args(key_file):
    return SimpleNamespace(key_fd=None, key_file=str(key_file))


def _fake_service(monkeypatch, log, new_key="art_new_0123456789abcdefghij", delay=0.0):
    """`_request` that records each call (one appended line, safe across forked processes)."""

    def fake(method, url, key, payload, *a, **kw):
        with open(log, "a") as handle:
            handle.write(f"{method} {url.rsplit('/api/v1', 1)[-1]}\n")
        if url.endswith("/me"):
            return {"rotating": True}
        time.sleep(delay)
        return {"key": new_key}

    monkeypatch.setattr(client, "_request", fake)
    monkeypatch.setattr(client, "_api_base", lambda: "http://ream.test/api/v1")


def _calls(log):
    return log.read_text().splitlines() if log.exists() else []


def test_a_swap_leaves_an_empty_owner_only_lockfile_beside_the_key(
    tmp_path, monkeypatch
):
    key_file = _stale_key_file(tmp_path)
    log = tmp_path / "calls"
    _fake_service(monkeypatch, log)
    assert (
        client.maybe_rotate(_args(key_file), "art_old_0123456789abcdefghij")
        == "art_new_0123456789abcdefghij"
    )
    lock = tmp_path / "key.lock"
    assert lock.exists() and lock.stat().st_size == 0
    assert oct(lock.stat().st_mode & 0o777) == "0o600"
    assert key_file.read_text().strip() == "art_new_0123456789abcdefghij"


def test_a_file_swapped_while_waiting_for_the_lock_is_used_not_rotated_again(
    tmp_path, monkeypatch, capsys
):
    key_file = _stale_key_file(tmp_path)
    log = tmp_path / "calls"
    _fake_service(monkeypatch, log)

    @contextlib.contextmanager
    def another_process_swaps_first(path):
        # What the lock's holder did while this process waited: a fresh file, a new key.
        client.save_key_file(path, "art_swapped_elsewhere_0123456789")
        yield

    monkeypatch.setattr(client, "_swap_lock", another_process_swaps_first)
    got = client.maybe_rotate(_args(key_file), "art_old_0123456789abcdefghij")
    assert got == "art_swapped_elsewhere_0123456789"
    assert _calls(log) == [], "no /me and no rotate once the file is fresh"
    assert key_file.read_text().strip() == "art_swapped_elsewhere_0123456789"
    out = capsys.readouterr()
    assert "art_swapped_elsewhere_0123456789" not in out.out + out.err


def test_a_lock_that_cannot_be_taken_in_time_skips_the_swap_with_a_note(
    tmp_path, monkeypatch, capsys
):
    key_file = _stale_key_file(tmp_path)
    log = tmp_path / "calls"
    _fake_service(monkeypatch, log)
    monkeypatch.setattr(client, "_SWAP_LOCK_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(client, "_SWAP_LOCK_POLL_SECONDS", 0.02)
    holder = os.open(tmp_path / "key.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        got = client.maybe_rotate(_args(key_file), "art_old_0123456789abcdefghij")
        waited = time.monotonic() - started
    finally:
        os.close(holder)
    assert got == "art_old_0123456789abcdefghij"
    assert 0.25 <= waited < 5
    assert _calls(log) == []
    assert key_file.read_text().strip() == "art_old_0123456789abcdefghij"
    err = capsys.readouterr().err
    assert "weekly key swap did not happen" in err
    assert "the current key still works" in err


def _swap_in_a_child(key_file, barrier, results):
    # Runs in a forked process, so the parent's patched `_request` comes along.
    barrier.wait(timeout=20)
    results.put(client.maybe_rotate(_args(key_file), "art_old_0123456789abcdefghij"))


def test_two_processes_swapping_at_once_rotate_exactly_once(tmp_path, monkeypatch):
    key_file = _stale_key_file(tmp_path)
    log = tmp_path / "calls"
    # The rotate takes long enough that, without the lock, both would be inside it together.
    _fake_service(monkeypatch, log, delay=0.5)
    ctx = multiprocessing.get_context("fork")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    procs = [
        ctx.Process(target=_swap_in_a_child, args=(key_file, barrier, results))
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    got = [results.get(timeout=30) for _ in procs]
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0
    assert _calls(log).count("POST /keys/rotate") == 1
    assert got == ["art_new_0123456789abcdefghij", "art_new_0123456789abcdefghij"]
    assert key_file.read_text().strip() == "art_new_0123456789abcdefghij"
