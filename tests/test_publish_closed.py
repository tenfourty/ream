"""A publish can start closed to other agents, and every publish and update says who can read the
page (12 Nov 2026). A page is open to every agent by default, and a worker took "my seat" to mean
private for embargoed content; closing existed only after publishing, so the page had a window."""

import pytest
from fastapi.testclient import TestClient

from ream import keys, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body>{}</body></html>"
SECTIONED = PAGE.format('<section id="ideas"><p>drawn</p></section>')
OPEN_WORDS = "other agents can read this page"
CLOSED_WORDS = "only Jeremy and you can read this page"  # the suite's owner is Jeremy


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def seats(db):
    owner, _ = keys.mint(db, agent="owner")
    other, _ = keys.mint(db, agent="other")
    return {
        "owner": {"Authorization": f"Bearer {owner}"},
        "other": {"Authorization": f"Bearer {other}"},
    }


def _post_page(api, headers, *, route="pages", **more):
    payload = {"title": "Embargoed", "body": PAGE.format("secret"), "comments": "off"}
    return api.post(f"/api/v1/{route}", json=payload | more, headers=headers)


def test_every_publish_says_whether_other_agents_can_read_it(api, seats):
    made = _post_page(api, seats["owner"])
    assert made.status_code == 201, made.text
    assert made.json()["agents_can_read"] is True
    assert made.json()["agent_read"] == OPEN_WORDS
    seen = api.get(f"/api/v1/pages/{made.json()['id']}/body", headers=seats["other"])
    assert seen.status_code == 200


@pytest.mark.parametrize("route", ["pages", "artifacts"])
def test_a_page_can_be_published_closed_in_either_word_of_the_route(api, seats, route):
    made = _post_page(api, seats["owner"], route=route, agents_can_read=False)
    assert made.status_code == 201, made.text
    page_id = made.json()["id"]
    assert made.json()["agents_can_read"] is False
    assert made.json()["agent_read"] == CLOSED_WORDS
    for path in ("", "/body", "/versions", "/versions/1"):
        theirs = api.get(f"/api/v1/pages/{page_id}{path}", headers=seats["other"])
        missing = api.get(f"/api/v1/pages/{'b' * 26}{path}", headers=seats["other"])
        assert theirs.status_code == missing.status_code == 404, path
        assert theirs.json() == missing.json(), path
    own = api.get(f"/api/v1/pages/{page_id}/body", headers=seats["owner"])
    assert own.status_code == 200
    listed = api.get("/api/v1/pages?scope=all", headers=seats["other"]).json()
    assert page_id not in [row["id"] for row in listed]


def test_a_closed_publish_is_one_write_not_a_publish_then_a_close(
    db, api, seats, monkeypatch
):
    """No window: the page is stored closed. A publish that opened the page and closed it with
    the call that closes a page later would trip this."""

    def refuse(*_a, **_k):
        raise AssertionError("a closed publish must not publish open and close after")

    monkeypatch.setattr(store, "set_agents_can_read", refuse)
    made = _post_page(api, seats["owner"], agents_can_read=False)
    assert made.status_code == 201, made.text
    got = store.get_owned(db, "owner", made.json()["id"])
    assert got.agents_can_read is False and got.current_version == 1


def test_publishing_closed_in_the_store_stores_the_page_closed(db):
    closed = store.publish(db, "owner", "E", PAGE.format("s"), agents_can_read=False)
    assert closed.agents_can_read is False
    assert store.get_owned(db, "owner", closed.id).agents_can_read is False
    assert store.publish(db, "owner", "O", PAGE.format("o")).agents_can_read is True


def test_a_value_that_is_not_a_boolean_is_refused_and_stores_nothing(db, api, seats):
    refused = _post_page(api, seats["owner"], agents_can_read="not-a-flag")
    assert refused.status_code == 422
    assert store.list_for_agent(db, "owner") == []


def test_an_update_says_who_can_read_and_never_changes_it(api, seats):
    closed = _post_page(api, seats["owner"], agents_can_read=False).json()
    opened = _post_page(api, seats["owner"]).json()
    for made, flag, words in (
        (closed, False, CLOSED_WORDS),
        (opened, True, OPEN_WORDS),
    ):
        changed = api.patch(
            f"/api/v1/pages/{made['id']}",
            json={"body": PAGE.format("v2"), "expected_revision": made["revision"]},
            headers=seats["owner"],
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["agents_can_read"] is flag
        assert changed.json()["agent_read"] == words
        same = api.patch(
            f"/api/v1/pages/{made['id']}",
            json={
                "body": PAGE.format("v2"),
                "expected_revision": changed.json()["revision"],
            },
            headers=seats["owner"],
        )
        assert same.json()["unchanged"] is True
        assert same.json()["agents_can_read"] is flag
        got = api.get(f"/api/v1/pages/{made['id']}", headers=seats["owner"]).json()
        assert got["agents_can_read"] is flag


def test_a_section_update_says_who_can_read_too(api, seats):
    made = _post_page(api, seats["owner"], body=SECTIONED, agents_can_read=False).json()
    changed = api.patch(
        f"/api/v1/pages/{made['id']}/sections/ideas",
        json={"op": "replace", "html": "<p>new</p>"},
        headers=seats["owner"],
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["agents_can_read"] is False
    assert changed.json()["agent_read"] == CLOSED_WORDS


def test_a_copy_of_a_closed_page_is_closed_from_its_first_version(db, api, seats):
    made = _post_page(api, seats["owner"], agents_can_read=False).json()
    copy = api.post(f"/api/v1/pages/{made['id']}/duplicate", headers=seats["owner"])
    assert copy.status_code == 201, copy.text
    assert copy.json()["agents_can_read"] is False
    assert copy.json()["agent_read"] == CLOSED_WORDS
    assert store.get_owned(db, "owner", copy.json()["id"]).agents_can_read is False
    open_one = _post_page(api, seats["owner"]).json()
    open_copy = api.post(
        f"/api/v1/pages/{open_one['id']}/duplicate", headers=seats["owner"]
    )
    assert open_copy.json()["agents_can_read"] is True
