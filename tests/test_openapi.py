"""The served API contract.

An agent that cannot read this repository has to learn the API from somewhere.
Until 19 Sep 2026 the schema sat at FastAPI's default /openapi.json, outside
the /api/v1/* path Authelia bypasses for agent keys, so asking for it returned
a 302 to a login no key can complete -- which reads as one step short of
working, over and over, rather than as "not here". The first client for this
service was therefore written by reading api.py, decisions.py and store.py by
hand, which is exactly the drift a served schema prevents.
"""

from fastapi.testclient import TestClient

from artifacts.app import create_gallery_app


def test_the_schema_is_served_from_inside_the_api_prefix():
    app = create_gallery_app(db=None)
    assert app.openapi_url == "/api/v1/openapi.json"


def test_the_schema_answers_without_a_key_and_names_the_real_routes(db):
    """No key required: the schema is a contract, not data."""
    client = TestClient(create_gallery_app(db=db))

    response = client.get("/api/v1/openapi.json")

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


def test_the_default_path_is_not_served(db):
    """Nothing answers at /openapi.json any more, so the confusing redirect
    cannot come back by accident."""
    client = TestClient(create_gallery_app(db=db))

    assert client.get("/openapi.json").status_code == 404


def test_the_interactive_docs_stay_off():
    """The machine-readable contract is the point. An interactive UI would be a
    second surface with no reader."""
    app = create_gallery_app(db=None)
    assert app.docs_url is None and app.redoc_url is None
