import logging

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


def _padded_body(total_bytes: int) -> str:
    """PAGE followed by ASCII filler, sized to `total_bytes` UTF-8 bytes exactly."""
    filler = total_bytes - len(PAGE.encode("utf-8"))
    assert filler >= 0
    return PAGE + ("a" * filler)


def test_publishing_returns_a_one_label_url(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    response = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    )
    assert response.status_code == 201
    body = response.json()
    assert body["url"] == f"https://{body['id']}.tenfourty.com/"
    assert body["id"].isalnum() and body["id"].islower() and len(body["id"]) == 26


def test_patching_keeps_the_same_url_and_advances_the_version(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    second = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={
            "body": PAGE.replace("hi", "hello"),
            "expected_revision": first["revision"],
        },
        headers=auth(token),
    )
    assert second.status_code == 200
    assert second.json()["url"] == first["url"]
    assert second.json()["version"] == first["version"] + 1


def test_a_stale_revision_is_refused(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={"body": PAGE, "expected_revision": first["revision"]},
        headers=auth(token),
    )
    stale = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={"body": PAGE, "expected_revision": first["revision"]},
        headers=auth(token),
    )
    assert stale.status_code == 409


def test_a_body_with_a_secret_is_refused_and_says_where_not_what(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    response = client.post(
        "/api/v1/artifacts",
        json={"title": "Leaky", "body": PAGE + secret},
        headers=auth(token),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == {"refused": [{"shape": "aws_access_key_id", "line": 1}]}
    assert secret not in response.text


def test_another_agents_key_cannot_read_or_patch(client, db):
    token_a, _ = keys.mint(db, agent="seat-a")
    token_b, _ = keys.mint(db, agent="seat-b")
    mine = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers=auth(token_a),
    ).json()
    assert (
        client.get(f"/api/v1/artifacts/{mine['id']}", headers=auth(token_b)).status_code
        == 404
    )
    assert (
        client.patch(
            f"/api/v1/artifacts/{mine['id']}",
            json={"body": PAGE, "expected_revision": mine["revision"]},
            headers=auth(token_b),
        ).status_code
        == 404
    )
    assert mine["id"] not in client.get("/api/v1/artifacts", headers=auth(token_b)).text


def test_no_key_and_a_forged_identity_header_are_both_refused(client, db):
    assert (
        client.post("/api/v1/artifacts", json={"title": "x", "body": PAGE}).status_code
        == 401
    )
    forged = client.post(
        "/api/v1/artifacts",
        json={"title": "x", "body": PAGE},
        headers={"Remote-User": "jeremy", "Remote-Groups": "admins"},
    )
    assert forged.status_code == 401


def test_a_key_in_a_query_parameter_is_refused(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    response = client.post(
        f"/api/v1/artifacts?key={token}", json={"title": "x", "body": PAGE}
    )
    assert response.status_code == 401


def test_the_auth_scheme_is_case_insensitive(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    response = client.post(
        "/api/v1/artifacts",
        json={"title": "Board", "body": PAGE},
        headers={"Authorization": f"bEaReR {token}"},
    )
    assert response.status_code == 201


def test_a_body_at_the_exact_size_cap_is_accepted(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    body = _padded_body(store.MAX_BODY_BYTES)
    response = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": body}, headers=auth(token)
    )
    assert response.status_code == 201


def test_a_body_one_byte_over_the_cap_is_refused_with_413(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    body = _padded_body(store.MAX_BODY_BYTES + 1)
    response = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": body}, headers=auth(token)
    )
    assert response.status_code == 413


def test_a_patch_over_the_size_cap_is_refused_with_413_before_scanning(client, db):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    body = _padded_body(store.MAX_BODY_BYTES + 1)
    response = client.patch(
        f"/api/v1/artifacts/{first['id']}",
        json={"body": body, "expected_revision": first["revision"]},
        headers=auth(token),
    )
    assert response.status_code == 413


def test_a_secret_refusal_is_logged_without_the_secret_value(client, db, caplog):
    token, _ = keys.mint(db, agent="ops-seat")
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    with caplog.at_level(logging.WARNING, logger="artifacts.refusals"):
        client.post(
            "/api/v1/artifacts",
            json={"title": "Leaky", "body": PAGE + secret},
            headers=auth(token),
        )
    assert caplog.records
    for record in caplog.records:
        assert secret not in record.getMessage()
    messages = [record.getMessage() for record in caplog.records]
    assert any("ops-seat" in message for message in messages)
    assert any("aws_access_key_id" in message for message in messages)


def test_a_patch_refusal_is_logged_with_the_artifact_id(client, db, caplog):
    token, _ = keys.mint(db, agent="ops-seat")
    first = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=auth(token)
    ).json()
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    with caplog.at_level(logging.WARNING, logger="artifacts.refusals"):
        client.patch(
            f"/api/v1/artifacts/{first['id']}",
            json={"body": PAGE + secret, "expected_revision": first["revision"]},
            headers=auth(token),
        )
    messages = [record.getMessage() for record in caplog.records]
    assert any(first["id"] in message for message in messages)
    for message in messages:
        assert secret not in message
