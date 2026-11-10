"""An agent with a key can fetch the template for the job it is doing.

homelab-ij8.69. Jeremy chose the API alongside the skill directory rather than
instead of it, because a skill directory is a Claude Code notion and the brief
is that this has to work for "agents anywhere that I give access to". An agent
that is not Claude has no skill directory; it does have a key, because it needs
one to publish at all.
"""

import pytest
from fastapi.testclient import TestClient

from ream import keys, templates
from ream.app import create_gallery_app


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_the_index_lists_every_template_on_disk(client, db):
    token, _ = keys.mint(db, agent="any-seat")
    rows = client.get("/api/v1/templates", headers=auth(token)).json()["templates"]
    assert [r["name"] for r in rows] == templates.names()
    assert rows, "the index is empty, so every assertion below is vacuous"


def test_every_listed_template_says_what_it_is_for(client, db):
    """An index an agent cannot choose from is a directory listing.

    Asserted rather than assumed because the use-when text lives in a dict keyed
    by filename: adding a template and forgetting the entry is a one-line
    mistake that would otherwise ship a blank row.
    """
    token, _ = keys.mint(db, agent="any-seat")
    rows = client.get("/api/v1/templates", headers=auth(token)).json()["templates"]
    blank = [r["name"] for r in rows if not r["use_when"].strip()]
    assert not blank, f"listed with nothing saying when to use them: {blank}"
    assert all(r["title"].strip() for r in rows)
    assert all(r["bytes"] > 1000 for r in rows), (
        "a template that small is not a worked page"
    )


@pytest.mark.parametrize("name", templates.names())
def test_a_template_comes_back_byte_for_byte(client, db, name):
    token, _ = keys.mint(db, agent="any-seat")
    got = client.get(f"/api/v1/templates/{name}", headers=auth(token))
    assert got.status_code == 200
    assert got.text == templates.body(name)


@pytest.mark.parametrize("name", templates.names())
def test_a_template_is_served_as_source_never_as_a_page(client, db, name):
    """text/plain and nosniff, so the API origin can never render it.

    Published pages get their own throwaway origin precisely so a page cannot
    reach anything. Handing the same markup back as a renderable document from
    the API origin would quietly undo that, and the browser must not be left to
    guess -- hence nosniff as well as the type.
    """
    token, _ = keys.mint(db, agent="any-seat")
    got = client.get(f"/api/v1/templates/{name}", headers=auth(token))
    assert got.headers["content-type"].startswith("text/plain")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "text/html" not in got.headers["content-type"]


def test_an_unknown_name_is_a_404(client, db):
    token, _ = keys.mint(db, agent="any-seat")
    assert client.get("/api/v1/templates/nope", headers=auth(token)).status_code == 404


@pytest.mark.parametrize(
    "name",
    ["_shared", "..%2Fapi", "../api", "....//api", "%2e%2e%2fapi"],
)
def test_nothing_outside_the_template_directory_is_reachable(client, db, name):
    """Including `_shared`, which is on disk and is deliberately not a template.

    The name is matched against the directory listing rather than joined onto a
    path, so a traversal attempt is just a name that is not in the list. There
    is no separate rejection path that could be got wrong, and this proves the
    two cases land in the same place.
    """
    token, _ = keys.mint(db, agent="any-seat")
    got = client.get(f"/api/v1/templates/{name}", headers=auth(token))
    assert got.status_code == 404, got.text
    assert "BEGIN" not in got.text and "--paper" not in got.text


def test_without_a_key_nothing_is_served(client):
    assert client.get("/api/v1/templates").status_code == 401
    assert client.get("/api/v1/templates/project-status").status_code == 401


def test_a_revoked_key_stops_working(client, db):
    token, key_id = keys.mint(db, agent="going-away")
    assert client.get("/api/v1/templates", headers=auth(token)).status_code == 200
    keys.revoke(db, key_id, actor="admin")
    assert client.get("/api/v1/templates", headers=auth(token)).status_code == 401


def test_the_routes_are_in_the_published_contract(client, db):
    """The API describes itself, and the skill tells agents to prefer that to
    guessing -- so a route missing from the schema is a route nobody finds."""
    token, _ = keys.mint(db, agent="any-seat")
    paths = client.get("/api/v1/openapi.json", headers=auth(token)).json()["paths"]
    assert "/api/v1/templates" in paths
    assert "/api/v1/templates/{name}" in paths
