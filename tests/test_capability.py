import pytest
from fastapi.testclient import TestClient

from artifacts import keys, store
from artifacts.app import create_gallery_app
from artifacts.content import create_content_app

PAGE = "<!doctype html><html><head></head><body>hi</body></html>"
FRAMED = {"sec-fetch-dest": "iframe"}


@pytest.fixture()
def published(db):
    keys.mint(db, agent="ops-seat")
    return store.publish(db, "ops-seat", "Board", PAGE)


@pytest.fixture()
def content(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    return client


def test_the_page_is_served_only_with_its_own_token(db, content, published):
    token = store.read_token(db, published.id)
    assert content.get(f"/t/{token}/", headers=FRAMED).status_code == 200
    assert content.get("/t/not-the-token/", headers=FRAMED).status_code == 404
    assert content.get("/", headers=FRAMED).status_code == 404
    # a case-swap is a different token, byte for byte -- no case-folding anywhere in the path
    assert content.get(f"/t/{token.swapcase()}/", headers=FRAMED).status_code == 404
    # another artifact's token, presented on this artifact's host, opens nothing
    other = store.publish(db, "ops-seat", "Other", PAGE)
    assert content.get(f"/t/{store.read_token(db, other.id)}/", headers=FRAMED).status_code == 404


def test_a_version_needs_the_token_too(db, content, published):
    token = store.read_token(db, published.id)
    assert content.get(f"/t/{token}/v/1", headers=FRAMED).status_code == 200
    assert content.get("/t/not-the-token/v/1", headers=FRAMED).status_code == 404


def test_a_non_ascii_token_is_a_mismatch_not_a_crash(content):
    # hmac.compare_digest raises TypeError on a non-ASCII str; the comparison must be on bytes.
    assert content.get("/t/%C3%A9t%C3%A9/", headers=FRAMED).status_code == 404


def test_the_redirect_never_carries_the_token(db, content, published):
    token = store.read_token(db, published.id)
    response = content.get(f"/t/{token}/", follow_redirects=False)
    assert response.status_code == 303
    assert token not in response.headers["location"]
    assert response.headers["location"].endswith(f"/a/{published.id}")


def test_the_address_an_agent_is_given_still_works_as_a_link(content, published):
    response = content.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/a/{published.id}")


def test_the_agent_api_never_returns_the_token(db):
    token, _ = keys.mint(db, agent="ops-seat")
    client = TestClient(create_gallery_app(db))
    headers = {"authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/artifacts", json={"title": "Board", "body": PAGE}, headers=headers
    )
    artifact_id = created.json()["id"]
    read = store.read_token(db, artifact_id)
    patched = client.patch(
        f"/api/v1/artifacts/{artifact_id}",
        json={"body": PAGE.replace("hi", "hello"), "expected_revision": created.json()["revision"]},
        headers=headers,
    )
    listed = client.get("/api/v1/artifacts", headers=headers)
    fetched = client.get(f"/api/v1/artifacts/{artifact_id}", headers=headers)
    responses = (created, patched, listed, fetched)
    assert [r.status_code for r in responses] == [201, 200, 200, 200]
    for response in responses:
        assert read not in response.text


def test_the_token_survives_a_patch(db, published):
    before = store.read_token(db, published.id)
    store.patch(db, "ops-seat", published.id, PAGE.replace("hi", "hello"), published.revision)
    assert store.read_token(db, published.id) == before


def test_every_artifact_gets_its_own_unguessable_token(db):
    tokens = {store.read_token(db, store.publish(db, "ops-seat", "t", PAGE).id) for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(token) >= 32 for token in tokens)


def test_a_public_page_still_needs_its_token_in_this_part(db, content, published):
    """Fail-closed by ruling: a public page has no public viewer yet (see the task text)."""
    with db.connect() as conn:
        conn.execute("update artifact set visibility = 'public' where id = %s", (published.id,))
    assert content.get("/", headers=FRAMED).status_code == 404
    token = store.read_token(db, published.id)
    assert content.get(f"/t/{token}/", headers=FRAMED).status_code == 200


def test_content_url_is_what_the_gallery_frames(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    token = store.read_token(db, published.id)
    assert store.content_url(published.id, token) == (
        f"https://{published.id}.tenfourty.com/t/{token}/"
    )


def _fingerprint(response):
    """(status, body, content-type, every header except Date) -- Date is the one header
    that legitimately differs between two responses taken a moment apart."""
    headers = {k: v for k, v in response.headers.items() if k.lower() != "date"}
    return response.status_code, response.content, headers


def test_a_wrong_token_answers_exactly_like_a_missing_artifact(db, content, published):
    """Not just "both 404": the SAME status, body, content-type and header set, on a
    wrong token for a real artifact and on any token for a host with no artifact at all --
    so nothing here can be used to tell the two apart."""
    wrong_on_existing = content.get("/t/not-the-token/", headers=FRAMED)

    missing_host = TestClient(create_content_app(db))
    missing_host.headers.update({"host": "b" * 26 + ".tenfourty.com"})
    real_token_on_missing_host = missing_host.get(
        f"/t/{store.read_token(db, published.id)}/", headers=FRAMED
    )

    assert _fingerprint(wrong_on_existing) == _fingerprint(real_token_on_missing_host)


def test_no_trailing_slash_redirect_can_put_the_token_on_the_wire(db, content, published):
    """Starlette's `redirect_slashes` (on by default) would answer these with a 307 back
    to the tokenised URL, BEFORE the framed-only check, and its Location scheme follows
    the ASGI scope -- behind a TLS-terminating ingress uvicorn does not trust for
    X-Forwarded-Proto, that Location is `http://.../t/<tok>/`, putting the token on the
    wire in plaintext on an ordinary top-level navigation."""
    token = store.read_token(db, published.id)
    for path in (f"/t/{token}", f"/t/{token}/v/1/"):
        response = content.get(path, headers=FRAMED, follow_redirects=False)
        assert response.status_code == 404
        assert "location" not in response.headers
        assert token not in response.text
