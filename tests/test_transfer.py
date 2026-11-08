"""Moving a page from one agent to another without changing its address
(homelab-ij8.87)."""

import pytest
from fastapi.testclient import TestClient

from ream import decisions, events, keys, store
from ream.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>t</title></head><body><p>x</p></body></html>"
OPTIONS = [{"letter": "a", "label": "yes"}, {"letter": "b", "label": "no"}]


@pytest.fixture()
def api(db):
    return TestClient(create_gallery_app(db))


@pytest.fixture()
def seats(db):
    a, _ = keys.mint(db, agent="giver")
    b, _ = keys.mint(db, agent="taker")
    return {
        "giver": {"Authorization": f"Bearer {a}"},
        "taker": {"Authorization": f"Bearer {b}"},
    }


def test_an_agent_gives_its_page_and_the_history_follows(db, api, seats):
    page = store.publish(db, "giver", "Board", PAGE)
    asked = decisions.ask(
        db, agent="giver", artifact_id=page.id, question="q?", options=OPTIONS
    )
    decisions.answer(db, asked.id, letter="a", by="jeremy")
    moved = api.post(
        f"/api/v1/artifacts/{page.id}/transfer",
        json={"to": "taker"},
        headers=seats["giver"],
    )
    assert moved.json() == {"id": page.id, "from": "giver", "to": "taker"}
    assert store.get_owned(db, "taker", page.id).id == page.id
    assert [e.agent for e in events.list_for_agent(db, "taker")] != []
    assert events.list_for_agent(db, "giver") == [], (
        "the old owner is not woken about it"
    )
    assert (
        api.get(
            f"/api/v1/artifacts/{page.id}/decisions", headers=seats["taker"]
        ).json()[0]["id"]
        == asked.id
    )


def test_nobody_takes_a_page_that_is_not_theirs(db, api, seats):
    page = store.publish(db, "giver", "Board", PAGE)
    taken = api.post(
        f"/api/v1/artifacts/{page.id}/transfer",
        json={"to": "taker"},
        headers=seats["taker"],
    )
    assert taken.status_code == 404
    assert store.get_owned(db, "giver", page.id).agent == "giver"


def test_the_destination_must_be_a_connected_machine(db, api, seats):
    page = store.publish(db, "giver", "Board", PAGE)
    refused = api.post(
        f"/api/v1/artifacts/{page.id}/transfer",
        json={"to": "nobody"},
        headers=seats["giver"],
    )
    assert refused.status_code == 422


def test_he_can_move_any_page_from_its_menu(db, api, seats):
    page = store.publish(db, "giver", "Board", PAGE)
    assert "Move to another agent" in api.get(f"/a/{page.id}").text
    assert "taker" in api.get(f"/a/{page.id}/move").text
    done = api.post(
        f"/a/{page.id}/move",
        data={"to": "taker"},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert "Moved from giver to taker." in done.text
    cross = api.post(
        f"/a/{page.id}/move",
        data={"to": "giver"},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert cross.status_code == 403


def test_the_vault_spelling_of_an_agent_reaches_the_same_agent(db, api):
    """The homelab ops seat, 24 Sep 2026: the page list says homelab-ops, the
    vault says homelab_ops, and the second was refused as though the agent did
    not exist."""
    a, _ = keys.mint(db, agent="baba-operator")
    keys.mint(db, agent="homelab-ops")
    page = store.publish(db, "baba-operator", "Board", PAGE)
    moved = api.post(
        f"/api/v1/artifacts/{page.id}/transfer",
        json={"to": "homelab_ops"},
        headers={"Authorization": f"Bearer {a}"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["to"] == "homelab-ops", "the real name, not the spelling"
    assert store.get_owned(db, "homelab-ops", page.id).id == page.id


def test_two_agents_one_spelling_apart_are_never_guessed_between(db, api):
    a, _ = keys.mint(db, agent="giver")
    keys.mint(db, agent="seat-one")
    keys.mint(db, agent="Seat_One")
    page = store.publish(db, "giver", "Board", PAGE)
    refused = api.post(
        f"/api/v1/artifacts/{page.id}/transfer",
        json={"to": "seat_one"},
        headers={"Authorization": f"Bearer {a}"},
    )
    assert refused.status_code == 422
    assert "page list" in refused.json()["detail"]
    assert store.get_owned(db, "giver", page.id).agent == "giver"
