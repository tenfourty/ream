"""The publisher reads back its own page.

An author cannot see its rendered page here: the content origin answers only a
framed request holding a capability token no agent is given, and the gallery
sits behind Jeremy's login. Before this route existed the only thing a
publisher could verify about its own work was the title -- which is why the
first board on this service carried a `PAGE TITLE` placeholder in `<title>`
for two hours with nobody able to notice.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import keys, store
from artifacts.app import create_gallery_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_the_body_comes_back_byte_for_byte(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    published = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()

    response = client.get(f"/api/v1/artifacts/{published['id']}/body", headers=auth(token))

    assert response.status_code == 200
    read = response.json()
    assert read["body"] == PAGE
    assert read["id"] == published["id"]
    assert read["version"] == 1
    assert read["revision"] == published["revision"]


def test_it_returns_the_current_version_after_a_patch(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    updated = PAGE.replace("hi", "hello")
    second = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={"body": updated, "expected_revision": first["revision"]},
        headers=auth(token),
    ).json()

    read = client.get(
        f"/api/v1/artifacts/{first['id']}/body", headers=auth(token)
    ).json()

    assert read["body"] == updated
    assert read["version"] == 2
    assert read["revision"] == second["revision"]


def test_the_revision_it_returns_is_one_a_patch_accepts(client, db):
    """The read-back is only a self-check loop if what it hands back can be
    used to write the next version -- otherwise an agent has to call two routes
    to do one thing."""
    token, _ = keys.mint(db, agent="ops-seat")
    published = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    read = client.get(
        f"/api/v1/artifacts/{published['id']}/body", headers=auth(token)
    ).json()

    response = client.patch(
        f"/api/v1/artifacts/{published['id']}",
        json={"body": read["body"] + "<!-- checked -->", "expected_revision": read["revision"]},
        headers=auth(token),
    )

    assert response.status_code == 200


def test_another_agents_page_is_a_plain_404(client, db):
    """Never a 403: an agent learns nothing about what it does not own, not
    even that it exists. Same discipline as every other owning route."""
    mine, _ = keys.mint(db, agent="ops-seat")
    theirs, _ = keys.mint(db, agent="someone-else")
    published = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(mine)
    ).json()
    store.set_agents_can_read(db, published["id"], False, agent=None)  # closed to other agents: homelab-ij8.105 made pages readable by default

    response = client.get(
        f"/api/v1/artifacts/{published['id']}/body", headers=auth(theirs)
    )

    assert response.status_code == 404
    assert PAGE not in response.text


def test_an_unknown_id_is_a_404(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    response = client.get(
        "/api/v1/artifacts/aaaaaaaaaaaaaaaaaaaaaaaaaa/body", headers=auth(token)
    )
    assert response.status_code == 404


def test_it_needs_a_key_at_all(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    published = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()

    response = client.get(f"/api/v1/artifacts/{published['id']}/body")

    assert response.status_code == 401
    assert PAGE not in response.text


def test_a_revoked_key_cannot_read_a_body_back(client, db):
    token, key_id = keys.mint(db, agent="ops-seat")
    published = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    keys.revoke(db, key_id)

    response = client.get(f"/api/v1/artifacts/{published['id']}/body", headers=auth(token))

    assert response.status_code == 401
    assert PAGE not in response.text


def test_a_page_published_before_the_advisory_existed_still_reports_itself(client, db):
    """The read-back re-runs the CSP check on what is stored, so a page that
    was published when nothing checked still tells its author what will not
    run -- without republishing it."""
    token, _ = keys.mint(db, agent="ops-seat")
    published = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": "<!doctype html>\n<iframe></iframe>\n"},
        headers=auth(token),
    ).json()

    read = client.get(
        f"/api/v1/artifacts/{published['id']}/body", headers=auth(token)
    ).json()

    assert [w["shape"] for w in read["warnings"]] == ["iframe"]


# --- a wrong guess must not look like a missing feature ----------------------


def test_a_wrong_route_name_says_which_ones_are_real(db):
    """On 21 Sep a seat guessed /source, /content, /html, /versions, /revisions
    and /history, got a bare 404 from every one, and concluded the capability
    did not exist -- leaving a live page it owned unupdated. `/body` had been
    there since 19 Sep.

    Its own diagnosis is what this test defends: "the failure mode of guessing
    is indistinguishable from the failure mode of a missing capability -- every
    wrong guess returns exactly what a missing feature returns." So the first
    404 carries the answer, which is the only moment the reader is certain to
    be looking.
    """
    token, _ = keys.mint(db, agent="seat")
    published = store.publish(db, "seat", "Board", PAGE)
    client = TestClient(create_gallery_app(db))

    # "versions" has since been removed from this list because it became a REAL
    # route (homelab-ij8.30): one of the six things that seat reached for now
    # exists, which is the better way to answer a wrong guess. The other five
    # still get pointed at /body.
    for guess in ("source", "content", "html", "revisions", "history"):
        got = client.get(
            f"/api/v1/artifacts/{published.id}/{guess}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert got.status_code == 404
        detail = got.json()["detail"]
        assert "body" in detail["routes"], f"{guess}: the 404 does not name /body"

    # The six names a person actually reaches for get pointed straight at it.
    pointed = client.get(
        f"/api/v1/artifacts/{published.id}/source",
        headers={"Authorization": f"Bearer {token}"},
    ).json()["detail"]
    assert pointed["did_you_mean"], "the obvious synonyms get a direct answer"
    assert "body" in pointed["did_you_mean"]

    # And the guess that turned out to be a real capability answers as one.
    real = client.get(
        f"/api/v1/artifacts/{published.id}/versions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert real.status_code == 200
    assert [row["n"] for row in real.json()] == [1]


def test_the_catch_all_never_shadows_a_real_route(db):
    """It is declared last so it can only catch a miss. If it ever moved above
    the real routes they would all answer 404 while passing their own tests in
    isolation -- the same trap /artifacts/deleted hit."""
    token, _ = keys.mint(db, agent="seat")
    published = store.publish(db, "seat", "Board", PAGE)
    client = TestClient(create_gallery_app(db))
    auth = {"Authorization": f"Bearer {token}"}

    assert client.get(f"/api/v1/artifacts/{published.id}/body", headers=auth).status_code == 200
    assert client.get(f"/api/v1/artifacts/{published.id}/decisions", headers=auth).status_code == 200
    assert client.get(f"/api/v1/artifacts/{published.id}/comments", headers=auth).status_code == 200
