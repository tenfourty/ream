"""Which seat am I, and what may I do — in one request.

Proposed by the Warpgate seat on 20 Sep 2026 after the answer cost an afternoon.
Every agent on the management machine reads ~/.config/artifacts/key, which belongs to ONE seat, so
a key holder's belief about which seat it is can be wrong for weeks with nothing
saying so — and a grant then gets applied to an identity nobody is using. That
is exactly what happened: `decisions?scope=all` returned 403, the grant existed,
and it was on a different seat.
"""

import pytest
from fastapi.testclient import TestClient

from ream import grants, keys
from ream.app import create_gallery_app


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_me_names_the_seat_and_the_key(client, db):
    token, key_id = keys.mint(db, agent="warpgate-arc")
    body = client.get("/api/v1/me", headers=auth(token)).json()
    assert body["agent"] == "warpgate-arc"
    assert body["key_id"] == key_id, "the id artifacts-admin list-keys prints"
    assert body["scopes"] == []


def test_me_lists_the_seats_scopes(client, db):
    token, _ = keys.mint(db, agent="homelab-ops")
    grants.grant(db, "homelab-ops", grants.READ_ALL_DECISIONS)
    body = client.get("/api/v1/me", headers=auth(token)).json()
    assert body["scopes"] == [grants.READ_ALL_DECISIONS]


def test_me_never_shows_another_seats_scopes(client, db):
    """The restriction is in the SQL, not in a caller's filter."""
    grants.grant(db, "other-seat", grants.READ_ALL_DECISIONS)
    token, _ = keys.mint(db, agent="quiet-seat")
    body = client.get("/api/v1/me", headers=auth(token)).json()
    assert body["scopes"] == []
    assert "other-seat" not in str(body)


def test_me_never_echoes_the_key_itself(client, db):
    token, _ = keys.mint(db, agent="seat")
    raw = client.get("/api/v1/me", headers=auth(token)).text
    assert token not in raw
    assert "art_" not in raw


def test_me_needs_a_key_like_everything_else(client):
    assert client.get("/api/v1/me").status_code == 401


def test_the_403_on_scope_all_names_the_seat(client, db):
    """The message that sent two sessions hunting for a grant that existed.

    Naming the seat turns "which grant is missing" into "ah, I am not the seat I
    thought I was", which is the actual answer and is otherwise several steps of
    database archaeology away.
    """
    token, _ = keys.mint(db, agent="baba-operator")
    got = client.get("/api/v1/decisions?scope=all", headers=auth(token))
    assert got.status_code == 403
    detail = got.json()["detail"]
    assert "baba-operator" in detail, "the seat the key actually authenticates as"
    assert "/api/v1/me" in detail, "and where to look next"


def test_a_seat_with_the_grant_is_not_refused(client, db):
    """The control arm: a message change must not become a permission change."""
    token, _ = keys.mint(db, agent="homelab-ops")
    grants.grant(db, "homelab-ops", grants.READ_ALL_DECISIONS)
    assert (
        client.get("/api/v1/decisions?scope=all", headers=auth(token)).status_code
        == 200
    )


def test_me_is_in_the_published_contract(client, db):
    token, _ = keys.mint(db, agent="seat")
    paths = client.get("/api/v1/openapi.json", headers=auth(token)).json()["paths"]
    assert "/api/v1/me" in paths
