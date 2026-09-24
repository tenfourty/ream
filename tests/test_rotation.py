"""Machine keys replace themselves weekly (homelab-ij8.34, Jeremy's (3a) of
24 Sep 2026): the old key dies when the new one is first used, a key never
swapped is refused after two weeks, and a thief who swaps first locks the real
machine out -- loudly."""

import os
import time

import pytest
from fastapi.testclient import TestClient
from test_client import live  # noqa: F401

from artifacts import client, keys
from artifacts.app import create_gallery_app


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
            "update agent_key set created_at = now() - interval '15 days' where id = %s",
            (key_id,),
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
