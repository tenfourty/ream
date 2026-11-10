"""Moving an agent to a new machine is one approval (9 Nov 2026, docs/decisions.md): the
approve page's two choices, the retiring of old keys at the new key's first use, and the
agent's own twin, POST /api/v1/agents/me/move."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ream import keys
from ream.app import create_gallery_app

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
PAGE = "<!doctype html><title>t</title><h1>hi</h1>"


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


def _approve_as(gallery, code, choice, name="", headers=SAME_ORIGIN):
    page = gallery.get("/connect", params={"code": code}).text
    request_id = re.search(r'name="request_id" value="([^"]+)"', page).group(1)
    return gallery.post(
        "/connect/approve",
        data={"request_id": request_id, "code": code, "name": name, "choice": choice},
        headers=headers,
    )


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _pages(gallery, token):
    return gallery.get("/api/v1/pages", headers=_bearer(token))


def _publish(gallery, token):
    response = gallery.post(
        "/api/v1/pages", json={"title": "Board", "body": PAGE}, headers=_bearer(token)
    )
    assert response.status_code == 201, response.text
    return response.json()


def _count(db, sql):
    with db.connect() as conn:
        return conn.execute(sql).fetchone()[0]


def test_the_approve_page_offers_both_choices_only_for_a_taken_name(db, gallery):
    keys.mint(db, agent="laptop")
    taken = _ask(gallery, name="laptop")
    page = gallery.get("/connect", params={"code": taken["code"]}).text
    assert "Same agent, new machine" in page and "New agent" in page
    free = _ask(gallery, name="brand-new", address="198.51.100.7")
    page = gallery.get("/connect", params={"code": free["code"]}).text
    assert "Same agent, new machine" not in page and "Approve" in page


def test_same_agent_adds_the_key_and_the_pages_stay(db, gallery):
    old, _ = keys.mint(db, agent="laptop")
    made = _publish(gallery, old)
    started = _ask(gallery, name="laptop")
    done = _approve_as(gallery, started["code"], "same")
    assert "Approved as laptop" in done.text
    got = _poll(gallery, started)
    assert got["status"] == "approved" and got["agent"] == "laptop"
    listed = _pages(gallery, got["key"]).json()
    assert made["id"] in str(listed)
    assert _count(db, "select count(*) from agent_key where agent = 'laptop'") == 2
    assert _count(db, "select count(*) from event where kind = 'agent_moved'") == 1


def test_old_keys_work_until_the_new_keys_first_use_then_refuse(db, gallery):
    old, _ = keys.mint(db, agent="laptop")
    _publish(gallery, old)
    started = _ask(gallery, name="laptop")
    _approve_as(gallery, started["code"], "same")
    new = _poll(gallery, started)["key"]
    assert _pages(gallery, old).status_code == 200, (
        "both work until the new one is used"
    )
    assert gallery.get("/api/v1/me", headers=_bearer(new)).status_code == 200
    refused = _pages(gallery, old)
    assert refused.status_code == 401
    assert "moved to a new machine" in refused.json()["detail"]
    assert _pages(gallery, new).status_code == 200
    gallery.get("/api/v1/me", headers=_bearer(new))  # idempotent: no second event
    assert (
        _count(db, "select count(*) from event where kind = 'agent_keys_retired'") == 1
    )


def test_new_agent_with_a_taken_name_is_refused_and_a_free_name_is_not(db, gallery):
    keys.mint(db, agent="laptop")
    keys.mint(db, agent="other")
    started = _ask(gallery, name="laptop")
    taken = _approve_as(gallery, started["code"], "new", name="other")
    assert "already an agent" in taken.text and "Approved as" not in taken.text
    reserved = _approve_as(gallery, started["code"], "new", name="jeremy")
    assert "reserved" in reserved.text
    blank = _approve_as(gallery, started["code"], "new", name="")
    assert "Approved." not in blank.text
    same_name = _approve_as(gallery, started["code"], "new", name="laptop")
    assert "already an agent" in same_name.text
    ok = _approve_as(gallery, started["code"], "new", name="laptop-2")
    assert "Approved." in ok.text
    assert _poll(gallery, started)["agent"] == "laptop-2"


def test_same_agent_needs_his_own_page(db, gallery):
    old, _ = keys.mint(db, agent="laptop")
    started = _ask(gallery, name="laptop")
    foreign = _approve_as(
        gallery, started["code"], "same", headers={"sec-fetch-site": "cross-site"}
    )
    assert foreign.status_code == 403
    assert _poll(gallery, started) == {"status": "pending"}
    assert _pages(gallery, old).status_code == 200


def test_a_machines_own_request_never_becomes_a_move(db, gallery):
    keys.mint(db, agent="laptop")
    started = _ask(gallery, name="laptop")
    assert _poll(gallery, started) == {"status": "pending"}
    assert (
        _count(db, "select count(*) from connect_request where move is not null") == 0
    )


def _rotating(db, agent):
    token, key_id = keys.mint(db, agent=agent)
    with db.connect() as conn:
        conn.execute("update agent_key set rotating = true where id = %s", (key_id,))
    return token, key_id


def _start(gallery, token):
    return gallery.post("/api/v1/agents/me/move", headers=_bearer(token))


def _collect(gallery, token):
    return gallery.post("/api/v1/connect/poll", json={"poll_token": token}).json()


def test_an_agent_moves_itself_and_only_itself(db, gallery):
    mine, _ = _rotating(db, "laptop")
    other, _ = _rotating(db, "rival")
    made = _publish(gallery, mine)
    started = _start(gallery, mine)
    assert started.status_code == 201 and started.json()["agent"] == "laptop"
    token = started.json()["move_token"]
    got = _collect(gallery, token)
    assert got["status"] == "approved" and got["agent"] == "laptop"
    assert _collect(gallery, token) == {"status": "delivered"}
    assert _pages(gallery, mine).status_code == 200  # not retired until first use
    assert made["id"] in str(_pages(gallery, got["key"]).json())
    assert _pages(gallery, mine).status_code == 401
    assert _pages(gallery, other).status_code == 200
    rival = (
        "select count(*) from agent_key where agent = 'rival' and revoked_at is null"
    )
    assert _count(db, rival) == 1


def test_a_self_move_retires_only_the_calling_key(db, gallery):
    mine, _ = _rotating(db, "laptop")
    sibling, _ = _rotating(db, "laptop")
    got = _collect(gallery, _start(gallery, mine).json()["move_token"])
    gallery.get("/api/v1/me", headers=_bearer(got["key"]))
    assert _pages(gallery, mine).status_code == 401
    assert _pages(gallery, sibling).status_code == 200
    assert _pages(gallery, got["key"]).status_code == 200


def test_a_house_key_cannot_start_a_move(db, gallery):
    house, _ = keys.mint(db, agent="house-seat")
    assert _start(gallery, house).status_code == 409
    assert _count(db, "select count(*) from connect_request") == 0


def test_a_move_needs_a_key_and_a_second_ask_cancels_the_first(db, gallery):
    assert gallery.post("/api/v1/agents/me/move").status_code == 401
    mine, _ = _rotating(db, "laptop")
    first = _start(gallery, mine).json()
    second = _start(gallery, mine).json()
    assert _collect(gallery, first["move_token"]) == {"status": "denied"}
    assert _collect(gallery, second["move_token"])["status"] == "approved"


def test_an_expired_self_move_cannot_be_collected(db, gallery):
    mine, _ = _rotating(db, "laptop")
    token = _start(gallery, mine).json()["move_token"]
    with db.connect() as conn:
        conn.execute(
            "update connect_request set expires_at = %s",
            (datetime.now(UTC) - timedelta(seconds=1),),
        )
    assert _collect(gallery, token) == {"status": "expired"}
    assert _count(db, "select count(*) from agent_key where agent = 'laptop'") == 1


def test_an_owner_approved_request_that_expired_is_not_collected(db, gallery):
    keys.mint(db, agent="laptop")
    started = _ask(gallery, name="laptop")
    _approve_as(gallery, started["code"], "same")
    with db.connect() as conn:
        conn.execute(
            "update connect_request set expires_at = %s",
            (datetime.now(UTC) - timedelta(seconds=1),),
        )
    assert _poll(gallery, started) == {"status": "expired"}


def test_revoking_the_originating_key_kills_its_pending_move(db, gallery):
    mine, key_id = _rotating(db, "laptop")
    token = _start(gallery, mine).json()["move_token"]
    keys.revoke(db, key_id, actor="admin")
    assert _collect(gallery, token) == {"status": "denied"}
    assert _count(db, "select count(*) from agent_key where agent = 'laptop'") == 1


def test_the_originating_key_must_be_live_at_collect_time(db, gallery):
    mine, key_id = _rotating(db, "laptop")
    token = _start(gallery, mine).json()["move_token"]
    with db.connect() as conn:  # revoked by a path that skips keys.revoke
        conn.execute(
            "update agent_key set revoked_at = %s where id = %s",
            (datetime.now(UTC), key_id),
        )
    assert _collect(gallery, token) == {"status": "denied"}
