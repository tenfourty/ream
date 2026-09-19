"""The production entrypoint: a uvicorn `--factory` callable that reads
`DATABASE_URL`, migrates, and hands back the same fail-closed host router
`create_app` builds for every test in `test_router.py`. The two behaviours
that matter in production and never come up under pytest's own fixtures:
a missing/empty DSN must fail loudly without ever printing it (it carries the
database password), and the app it returns must answer `/healthz` only on the
gallery host -- exactly the router behaviour `test_router.py` already proves,
just reached through the real entrypoint this time.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts.serve import app_from_env


def test_missing_database_url_exits_without_leaking_anything(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        app_from_env()
    assert str(excinfo.value) == "DATABASE_URL is not set"


def test_empty_database_url_exits_without_leaking_anything(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(SystemExit) as excinfo:
        app_from_env()
    assert str(excinfo.value) == "DATABASE_URL is not set"


def test_returned_app_answers_healthz_only_on_the_gallery_host(dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://artifacts.tenfourty.site")
    client = TestClient(app_from_env())

    ok = client.get("/healthz", headers={"host": "artifacts.tenfourty.site"})
    assert ok.status_code == 200
    assert ok.json() == {"status": "ok"}

    other = client.get("/healthz", headers={"host": "evil.example"})
    assert other.status_code == 404


def test_app_from_env_migrates_so_the_schema_is_ready_on_first_request(dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", dsn)
    with TestClient(app_from_env()) as client:
        response = client.get(
            "/api/v1/artifacts",
            headers={
                "authorization": "Bearer nope",
                "host": "artifacts.tenfourty.site",
            },
        )
    # 401 (not 500) proves the schema exists: agent_key was queried, not missing.
    assert response.status_code == 401
