from fastapi.testclient import TestClient

from ream.app import create_gallery_app


def test_healthz_reports_ok():
    client = TestClient(create_gallery_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
