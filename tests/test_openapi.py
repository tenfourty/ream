"""The served API contract, and who may read it.

An agent that cannot read this repository has to learn the API from somewhere.
Until 19 Sep 2026 the schema sat at FastAPI's default /openapi.json, outside
the /api/v1/* path Authelia bypasses for agent keys, so asking for it returned
a 302 to a login no key can complete -- which reads as one step short of
working, over and over, rather than as "not here". The first client for this
service was therefore written by reading api.py, decisions.py and store.py by
hand, which is exactly the drift a served schema prevents.

Moving it under /api/v1 solves reachability and creates a second problem in
the same step: that prefix is bypassed, so a route there is unauthenticated.
The schema is therefore served by the agent router, behind the same key as the
data. Both halves are asserted here, because fixing either one alone is a
regression in the other.
"""

import pytest
from fastapi.testclient import TestClient

from ream import keys
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"


@pytest.fixture()
def client(db):
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_the_schema_is_reachable_with_a_key_and_names_the_real_routes(client, db):
    token, _ = keys.mint(db, agent="ops-seat")

    response = client.get("/api/v1/openapi.json", headers=auth(token))

    assert response.status_code == 200
    paths = response.json()["paths"]
    for route in (
        "/api/v1/artifacts",
        "/api/v1/artifacts/{artifact_id}",
        "/api/v1/artifacts/{artifact_id}/body",
        "/api/v1/artifacts/{artifact_id}/decisions",
        "/api/v1/events",
    ):
        assert route in paths, f"{route} missing from the served schema"
    assert "get" in paths["/api/v1/artifacts/{artifact_id}/body"]


def test_the_schema_needs_a_key(client):
    """The prefix it lives under is bypassed by Authelia, so this route's own
    check is the only thing between the shape of this API and any device on
    the local network."""
    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 401
    assert "paths" not in response.text


def test_a_revoked_key_cannot_read_the_schema(client, db):
    token, key_id = keys.mint(db, agent="ops-seat")
    keys.revoke(db, key_id, actor="admin")

    response = client.get("/api/v1/openapi.json", headers=auth(token))

    assert response.status_code == 401


def test_a_key_in_a_query_parameter_is_still_refused(client, db):
    """The same refusal every other route makes: a key in a URL lands in logs
    and history."""
    token, _ = keys.mint(db, agent="ops-seat")

    response = client.get(f"/api/v1/openapi.json?key={token}", headers=auth(token))

    assert response.status_code == 401


def test_fastapis_own_unauthenticated_schema_route_is_off(client):
    """If this ever answers, the authenticated route above has been bypassed
    rather than replaced."""
    assert client.get("/openapi.json").status_code == 404


def test_the_interactive_docs_stay_off(client):
    """The machine-readable contract is the point. An interactive UI would be a
    second surface with no reader."""
    app = create_gallery_app(db=None)
    assert app.docs_url is None and app.redoc_url is None
    assert app.openapi_url is None
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_a_revoked_keys_401_names_the_reason_it_was_revoked(client, db):
    token, key_id = keys.mint(db, agent="ops-seat")
    keys.revoke(db, key_id, actor="admin", reason="folded into cove")

    response = client.get("/api/v1/openapi.json", headers=auth(token))

    assert response.status_code == 401
    assert "folded into cove" in response.text
    assert token not in response.text
