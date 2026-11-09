"""Connecting a machine: it asks, Jeremy types its code and approves, it
collects a key nobody sees (homelab-ij8.102, his ruling of 23 Sep 2026)."""

import os
import re
import stat
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import connect, connect_cli, keys
from ream.app import create_gallery_app

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture()
def gallery(db):
    return TestClient(
        create_gallery_app(db), base_url="https://artifacts.tenfourty.site"
    )


def _ask(gallery, name="laptop", address="203.0.113.5"):
    response = gallery.post(
        "/api/v1/connect", json={"name": name}, headers={"x-real-ip": address}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _poll(gallery, started):
    return gallery.post(
        "/api/v1/connect/poll", json={"poll_token": started["poll_token"]}
    ).json()


def _approve(gallery, code, name="laptop", headers=SAME_ORIGIN):
    page = gallery.get("/connect", params={"code": code}).text
    request_id = re.search(r'name="request_id" value="([^"]+)"', page).group(1)
    return gallery.post(
        "/connect/approve",
        data={"request_id": request_id, "code": code, "name": name},
        headers=headers,
    )


def test_the_whole_flow_and_the_key_is_delivered_once(db, gallery):
    started = _ask(gallery)
    assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", started["code"])
    assert started["approve_at"] == "https://artifacts.tenfourty.site/connect"
    assert _poll(gallery, started) == {"status": "pending"}

    approved = _approve(gallery, started["code"])
    assert approved.status_code == 200 and "Approved" in approved.text
    assert "art_" not in approved.text, "he never sees the key"

    got = _poll(gallery, started)
    assert got["status"] == "approved" and got["agent"] == "laptop"
    me = gallery.get("/api/v1/me", headers={"Authorization": f"Bearer {got['key']}"})
    assert me.status_code == 200 and me.json()["agent"] == "laptop"
    # never twice
    assert _poll(gallery, started) == {"status": "delivered"}


def test_no_key_is_ever_stored_in_plaintext(db, gallery):
    started = _ask(gallery)
    _approve(gallery, started["code"])
    key = _poll(gallery, started)["key"]
    with db.connect() as conn:
        rows = conn.execute("select * from connect_request").fetchall()
    assert key not in repr(rows)


def test_he_can_type_the_code_loosely(db, gallery):
    started = _ask(gallery)
    loose = started["code"].replace("-", " ").lower()
    assert (
        "Approve this machine?" in gallery.get("/connect", params={"code": loose}).text
    )


def test_a_wrong_code_finds_nothing_and_says_so(gallery):
    _ask(gallery)
    page = gallery.get("/connect", params={"code": "AAAA-AAAA"}).text
    assert "No machine is waiting with that code" in page


def test_denied_means_nothing_is_delivered(db, gallery):
    started = _ask(gallery)
    page = gallery.get("/connect", params={"code": started["code"]}).text
    request_id = re.search(r'name="request_id" value="([^"]+)"', page).group(1)
    gallery.post(
        "/connect/deny",
        data={"request_id": request_id, "code": started["code"]},
        headers=SAME_ORIGIN,
    )
    assert _poll(gallery, started) == {"status": "denied"}


def test_an_unapproved_request_expires(db, gallery):
    started = _ask(gallery)
    with db.connect() as conn:
        conn.execute(
            "update connect_request set expires_at = %s",
            (datetime.now(UTC) - timedelta(seconds=1),),
        )
    assert _poll(gallery, started) == {"status": "expired"}
    assert (
        "No machine is waiting"
        in gallery.get("/connect", params={"code": started["code"]}).text
    )


def test_an_unknown_poll_token_looks_like_an_expired_one(gallery):
    assert gallery.post("/api/v1/connect/poll", json={"poll_token": "nope"}).json() == {
        "status": "expired"
    }


def test_an_open_door_is_a_limited_door(gallery):
    for i in range(connect.MAX_PENDING_PER_ADDRESS):
        _ask(gallery, name=f"vm-{i}", address="198.51.100.9")
    refused = gallery.post(
        "/api/v1/connect", json={"name": "x"}, headers={"x-real-ip": "198.51.100.9"}
    )
    assert refused.status_code == 429
    assert refused.json()["detail"].startswith("REFUSED")
    _ask(gallery, address="198.51.100.10")  # another address is unaffected


def test_five_machines_behind_one_address_can_all_wait(gallery):
    for i in range(5):
        _ask(gallery, name=f"office-{i}", address="198.51.100.20")


def test_the_same_name_from_one_address_replaces_its_earlier_request(db, gallery):
    first = _ask(gallery, name="office-1", address="198.51.100.21")
    for _ in range(connect.MAX_PENDING_PER_ADDRESS + 2):
        last = _ask(gallery, name="office-1", address="198.51.100.21")
    assert _poll(gallery, first) == {"status": "expired"}
    assert _poll(gallery, last)["status"] != "expired"
    with db.connect() as conn:
        live = conn.execute(
            "select count(*) from connect_request where expires_at > %s",
            (datetime.now(UTC),),
        ).fetchone()[0]
    assert live == 1


def test_the_global_ceiling_still_refuses(gallery, monkeypatch):
    monkeypatch.setattr(connect, "MAX_PENDING_TOTAL", 3)
    for i in range(3):
        _ask(gallery, name=f"m-{i}", address=f"198.51.100.{30 + i}")
    refused = gallery.post(
        "/api/v1/connect", json={"name": "x"}, headers={"x-real-ip": "198.51.100.99"}
    )
    assert refused.status_code == 429
    assert "too many requests" in refused.json()["detail"]


def test_a_name_already_connected_is_refused_until_its_key_is_revoked(db, gallery):
    _token, key_id = keys.mint(db, agent="laptop")
    started = _ask(gallery)
    refused = _approve(gallery, started["code"], name="laptop")
    assert "already the name of a connected machine" in refused.text
    keys.revoke(db, key_id)
    assert "Approved" in _approve(gallery, started["code"], name="laptop").text


def test_the_reserved_name_cannot_be_approved_for_a_machine(gallery):
    started = _ask(gallery)
    refused = _approve(gallery, started["code"], name="jeremy")
    assert "reserved" in refused.text and "Approved" not in refused.text
    assert "Approved" in _approve(gallery, started["code"], name="laptop").text


def test_approving_needs_his_own_page_not_another_site(gallery):
    started = _ask(gallery)
    forged = _approve(
        gallery, started["code"], headers={"sec-fetch-site": "cross-site"}
    )
    assert forged.status_code == 403
    assert _poll(gallery, started) == {"status": "pending"}


def test_the_starter_prompt_holds_no_secret_and_points_at_the_document(gallery):
    page = gallery.get("/connect").text
    assert "/api/v1/agents.txt" in page
    assert "art_" not in page
    assert page.count('<pre class="prompt">') == 1


def test_the_names_are_made_readable():
    assert connect.clean_name("Jeremy's MacBook Pro") == "jeremy-s-macbook-pro"
    assert connect.clean_name("--") is None
    assert connect.clean_name(None) is None


# --- the command an agent runs ----------------------------------------------


def test_the_command_saves_the_key_owner_only_and_never_prints_it(
    tmp_path, monkeypatch, capsys
):
    answers = iter(
        [
            {
                "code": "ACDE-FGHJ",
                "poll_token": "p",
                "expires_in": 900,
                "interval": 2,
                "approve_at": "https://a.example/connect",
            },
            {"status": "pending"},
            {"status": "approved", "agent": "laptop", "key": "art_SECRETSECRET"},
        ]
    )
    monkeypatch.setattr(connect_cli, "_post", lambda url, body: next(answers))
    monkeypatch.setattr(connect_cli.time, "sleep", lambda _s: None)
    key_file = tmp_path / "cfg" / "key"
    assert connect_cli.main(["--name", "laptop", "--key-file", str(key_file)]) == 0
    out = capsys.readouterr()
    assert "ACDE-FGHJ" in out.out and "Connected as laptop" in out.out
    assert "SECRET" not in out.out + out.err
    assert key_file.read_text() == "art_SECRETSECRET\n"
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600


def test_the_command_will_not_overwrite_a_key_without_force(
    tmp_path, monkeypatch, capsys
):
    key_file = tmp_path / "key"
    key_file.write_text("art_existing\n")
    monkeypatch.setattr(connect_cli, "_post", lambda *a: pytest.fail("must not ask"))
    assert connect_cli.main(["--key-file", str(key_file)]) == 1
    assert key_file.read_text() == "art_existing\n"
    assert "already has a key" in capsys.readouterr().err


def test_a_denied_request_saves_nothing(tmp_path, monkeypatch):
    answers = iter(
        [
            {
                "code": "ACDE-FGHJ",
                "poll_token": "p",
                "expires_in": 900,
                "interval": 2,
                "approve_at": "https://a.example/connect",
            },
            {"status": "denied"},
        ]
    )
    monkeypatch.setattr(connect_cli, "_post", lambda url, body: next(answers))
    monkeypatch.setattr(connect_cli.time, "sleep", lambda _s: None)
    key_file = tmp_path / "key"
    assert connect_cli.main(["--key-file", str(key_file)]) == 1
    assert not key_file.exists()


def test_the_code_boxes_are_an_upgrade_the_plain_field_still_works(gallery):
    page = gallery.get("/connect").text
    assert '<script src="/connect.js" defer></script>' in page
    assert 'name="code"' in page, "without scripts, the single field still submits"
    script = gallery.get("/connect.js")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert "http" not in script.text
