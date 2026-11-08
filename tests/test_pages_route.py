"""`/api/v1/pages` is the main route and `/api/v1/artifacts` the old one, kept for good.

Rename to Ream, step 2 (7 Nov 2026 decision: "its API says pages"). Both words are
registered against the same handlers, not a redirect, because agents POST. These tests
send the same request under each word and expect the same thing back.
"""

import pytest
from fastapi.testclient import TestClient

from ream import api, keys
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]
WORDS = ("pages", "artifacts")


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


@pytest.fixture()
def headers(db):
    token, _ = keys.mint(db, agent="ops-seat")
    return {"Authorization": f"Bearer {token}"}


def _publish(client, headers, word, title="Board"):
    response = client.post(
        f"/api/v1/{word}", json={"title": title, "body": PAGE}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_every_artifacts_route_has_a_pages_twin_and_the_other_way_round(db):
    paths = {route.path for route in api.agent_router(db).routes}
    old = {p for p in paths if "/artifacts" in p}
    assert len(old) > 20, "the old routes are still there"
    for path in old:
        new = path.replace("/artifacts", "/pages")
        assert new in paths, f"{path} has no {new}"
    for path in {p for p in paths if "/pages" in p}:
        assert path.replace("/pages", "/artifacts") in paths, path


def test_a_post_under_either_word_makes_a_page_either_word_can_read(client, headers):
    for word in WORDS:
        made = _publish(client, headers, word, title=f"via {word}")
        for read in WORDS:
            got = client.get(f"/api/v1/{read}/{made['id']}", headers=headers)
            assert got.status_code == 200, (word, read, got.text)
            assert got.json()["id"] == made["id"]
            assert got.json()["title"] == f"via {word}"


def test_a_get_answers_the_same_under_both_words(client, headers):
    made = _publish(client, headers, "pages")
    for suffix in ("", f"/{made['id']}", f"/{made['id']}/body", "/deleted"):
        new = client.get(f"/api/v1/pages{suffix}", headers=headers)
        old = client.get(f"/api/v1/artifacts{suffix}", headers=headers)
        assert new.status_code == old.status_code == 200, suffix
        assert new.text == old.text, suffix


def test_a_patch_and_the_shelf_posts_work_under_both_words(client, headers):
    for word, other in (("pages", "artifacts"), ("artifacts", "pages")):
        made = _publish(client, headers, word, title=f"start {word}")
        page = f"/api/v1/{word}/{made['id']}"
        seen = f"/api/v1/{other}/{made['id']}"

        patched = client.patch(
            page,
            json={
                "title": f"patched {word}",
                "body": PAGE,
                "expected_revision": made["revision"],
            },
            headers=headers,
        )
        assert patched.status_code == 200, patched.text
        assert client.get(seen, headers=headers).json()["title"] == f"patched {word}"

        renamed = client.post(
            f"{page}/rename", json={"title": "renamed"}, headers=headers
        )
        assert renamed.status_code == 200, renamed.text
        assert client.get(seen, headers=headers).json()["title"] == "renamed"

        archived = client.post(
            f"{page}/archive", json={"archived": True}, headers=headers
        )
        assert archived.status_code == 200, archived.text


def test_questions_are_shared_between_the_words(client, headers):
    made = _publish(client, headers, "pages")
    asked = client.post(
        f"/api/v1/pages/{made['id']}/decisions",
        json={"question": "ship?", "options": OPTIONS},
        headers=headers,
    )
    assert asked.status_code == 201, asked.text
    for word in WORDS:
        rows = client.get(f"/api/v1/{word}/{made['id']}/decisions", headers=headers)
        assert [r["id"] for r in rows.json()] == [asked.json()["id"]], word
    again = client.post(
        f"/api/v1/artifacts/{made['id']}/decisions",
        json={"question": "and then?", "options": OPTIONS},
        headers=headers,
    )
    assert again.status_code == 201, again.text
    rows = client.get(f"/api/v1/pages/{made['id']}/decisions", headers=headers).json()
    assert len(rows) == 2


def test_a_delete_under_one_word_is_undone_under_the_other(client, headers):
    for word, other in (("pages", "artifacts"), ("artifacts", "pages")):
        made = _publish(client, headers, word)
        gone = client.delete(f"/api/v1/{word}/{made['id']}", headers=headers)
        assert gone.status_code == 200, gone.text
        assert gone.json() == {"id": made["id"], "deleted": True}
        seen = client.get(f"/api/v1/{other}/{made['id']}", headers=headers)
        assert seen.status_code == 404
        binned = client.get(f"/api/v1/{other}/deleted", headers=headers).json()
        assert made["id"] in {row["id"] for row in binned}
        back = client.post(f"/api/v1/{other}/{made['id']}/restore", headers=headers)
        assert back.status_code == 200, back.text
        live = client.get(f"/api/v1/{word}/{made['id']}", headers=headers)
        assert live.status_code == 200


def test_a_folder_lists_its_pages_under_both_words(client, headers):
    made = client.post(
        "/api/v1/pages",
        json={"title": "In a folder", "body": PAGE, "folder": "roadmap"},
        headers=headers,
    )
    assert made.status_code == 201, made.text
    new = client.get("/api/v1/folders/roadmap/pages", headers=headers)
    old = client.get("/api/v1/folders/roadmap/artifacts", headers=headers)
    assert new.status_code == old.status_code == 200
    assert new.json() == old.json()
    assert [row["id"] for row in new.json()] == [made.json()["id"]]


def test_both_words_refuse_a_missing_key_and_a_stranger_alike(client, headers, db):
    made = _publish(client, headers, "pages")
    token, _ = keys.mint(db, agent="stranger")
    stranger = {"Authorization": f"Bearer {token}"}
    for word in WORDS:
        assert client.get(f"/api/v1/{word}").status_code == 401
        # Not yours answers exactly as no such page, under either word.
        missing = client.get(f"/api/v1/{word}/nonesuch", headers=stranger)
        theirs = client.patch(
            f"/api/v1/{word}/{made['id']}",
            json={"title": "x", "body": PAGE, "expected_revision": made["revision"]},
            headers=stranger,
        )
        assert missing.status_code == theirs.status_code == 404


def test_an_unknown_subroute_answers_the_same_under_both_words(client, headers):
    made = _publish(client, headers, "pages")
    got = [
        client.get(f"/api/v1/{word}/{made['id']}/source", headers=headers)
        for word in WORDS
    ]
    assert got[0].status_code == got[1].status_code == 404
    assert got[0].json() == got[1].json()


def test_the_served_schema_names_the_pages_routes_too(client, headers):
    paths = client.get("/api/v1/openapi.json", headers=headers).json()["paths"]
    for route in (
        "/api/v1/pages",
        "/api/v1/pages/{artifact_id}",
        "/api/v1/pages/{artifact_id}/body",
    ):
        assert route in paths, route
        assert route.replace("/pages", "/artifacts") in paths
