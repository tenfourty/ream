"""The discovery file (runcove-5t3oj.28): the onboarding document as data, at
/api/v1/agent.json and /.well-known/agent.json. Every office asked for one thing
above all (docs/agent/feedback/2026-10-23-discovery-file.md): its hosts come from
configuration, never from the request, because a client follows it and sends its
key where it points.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import keys
from artifacts.app import create_app

GALLERY_HOST = "artifacts.tenfourty.site"
API_HOST = "agents.example.com"


@pytest.fixture()
def client(db) -> TestClient:
    return TestClient(create_app(db))


@pytest.fixture()
def api_host(monkeypatch):
    monkeypatch.setenv("ARTIFACTS_API_HOST", API_HOST)
    return API_HOST


@pytest.fixture()
def wheel(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACTS_TOOLS_DIR", str(tmp_path))
    (tmp_path / "artifacts-0.1.0.post20261023080000-py3-none-any.whl").write_bytes(
        b"PK"
    )


def test_an_agent_on_the_lan_reads_it_without_a_key(client, wheel):
    response = client.get("/api/v1/agent.json", headers={"host": GALLERY_HOST})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    card = response.json()
    assert card["hosts"][0]["base_url"] == f"https://{GALLERY_HOST}"
    assert card["api"]["auth"] == {
        "scheme": "bearer",
        "header": "Authorization",
        "how_to_get_a_key": "see the onboarding document; never in a URL",
    }
    assert card["version"] == "0.1.0.post20261023080000"
    assert f"https://{GALLERY_HOST}/api/v1/tools/" in card["cli"]["install"]


def test_it_lists_the_routes_actually_served(client):
    card = client.get("/api/v1/agent.json", headers={"host": GALLERY_HOST}).json()
    routes = {(r["method"], r["path"]) for r in card["api"]["routes"]}

    assert ("POST", "/api/v1/artifacts") in routes
    assert ("GET", "/api/v1/agent.json") in routes
    assert ("POST", "/api/v1/decisions/{decision_id}/record") in routes
    assert not any(method == "HEAD" for method, _ in routes)
    assert not any(not path.startswith("/api/v1") for _, path in routes), (
        "only the agent API; never a gallery route"
    )


def test_a_forged_host_never_reaches_the_card(client):
    """The router answers only its configured names, so a forged Host gets the
    router's 404 and never a card that names it."""
    response = client.get("/api/v1/agent.json", headers={"host": "evil.example"})
    assert response.status_code == 404
    assert "evil.example" not in response.text


def test_the_hosts_come_from_configuration_not_the_request(client, api_host):
    """A port or a case variant of a configured name still routes there, and the
    card still names the configured value, not what was sent."""
    card = client.get(
        "/api/v1/agent.json", headers={"host": f"{GALLERY_HOST.upper()}:8443"}
    ).json()
    bases = [h["base_url"] for h in card["hosts"]]
    assert bases == [f"https://{GALLERY_HOST}", f"https://{API_HOST}"]


def test_the_public_face_never_names_the_house_name(client, api_host):
    """As agents.txt there does not: the internal name may only resolve inside."""
    for path in ("/api/v1/agent.json", "/.well-known/agent.json"):
        response = client.get(path, headers={"host": api_host})
        assert response.status_code == 200, path
        assert GALLERY_HOST not in response.text, path
        assert response.json()["hosts"] == [
            {
                "base_url": f"https://{API_HOST}",
                "reach": "the public API host: the agent API only, from anywhere",
            }
        ]


def test_the_well_known_path_answers_on_the_gallery_app_too(client):
    """Behind Authelia at the ingress there, so agents use /api/v1/agent.json on
    that name; the app still answers the same card."""
    well_known = client.get("/.well-known/agent.json", headers={"host": GALLERY_HOST})
    twin = client.get("/api/v1/agent.json", headers={"host": GALLERY_HOST})
    assert well_known.status_code == 200
    assert well_known.json() == twin.json()


def test_it_carries_no_key(client, db):
    token, _ = keys.mint(db, agent="outside-seat")
    body = client.get("/api/v1/agent.json", headers={"host": GALLERY_HOST}).text
    assert token not in body
    assert "art_" not in body


def test_without_a_tool_package_there_is_no_dead_install_line(
    client, monkeypatch, tmp_path
):
    monkeypatch.setenv("ARTIFACTS_TOOLS_DIR", str(tmp_path / "missing"))
    card = client.get("/api/v1/agent.json", headers={"host": GALLERY_HOST}).json()
    assert card["cli"]["install"] is None
    assert card["version"] is None


def test_agents_txt_points_at_it(client):
    body = client.get("/api/v1/agents.txt", headers={"host": GALLERY_HOST}).text
    assert f"https://{GALLERY_HOST}/api/v1/agent.json" in body
