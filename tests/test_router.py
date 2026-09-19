"""The router between the gallery host and the content hosts is the only code
between a content-domain request -- which never passes through Authelia -- and
the gallery app, which has no in-app identity check of its own (Authelia gates
it at ITS ingress). A router that fails open on anything it does not recognise
turns that boundary into a boundary in name only, so every row here asserts
WHICH app answered, not just a status code.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import keys, store
from artifacts.app import create_app

GALLERY_HOST = "artifacts.tenfourty.site"
FRAMED = {"sec-fetch-dest": "iframe"}

# Rows are (host template, expected). `{id}` / `{id_upper}` / `{short}` are filled in
# per-test from a real published id -- the id has to come from the `published_id` fixture
# (it is random), so the rows here can only be templates, not literal hosts.
ROWS = [
    (GALLERY_HOST, "gallery"),
    (f"{GALLERY_HOST}:443", "gallery"),
    (GALLERY_HOST.upper(), "gallery"),
    (f"{GALLERY_HOST}.", "gallery"),
    ("{id}.tenfourty.com", "content"),
    ("{id}.tenfourty.com:8443", "content"),
    ("{id_upper}.TENFOURTY.COM", "content"),
    ("{id}.TenFourty.com", "content"),
    ("{id}.tenfourty.com.", "content"),
    ("tenfourty.com", "router"),
    (f"{GALLERY_HOST}.tenfourty.com", "router"),
    ("{id}.tenfourty.com.evil.example", "router"),
    ("a.{id}.tenfourty.com", "router"),
    ("{short}.tenfourty.com", "router"),
    ("evil.example", "router"),
    ("[::1]:8000", "router"),
]


@pytest.fixture()
def published_id(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    keys.mint(db, agent="ops-seat")
    artifact = store.publish(db, "ops-seat", "Board", "<!doctype html><body>hi</body></html>")
    return artifact.id


@pytest.fixture()
def token(db, published_id):
    return store.read_token(db, published_id)


@pytest.fixture()
def client(db, published_id) -> TestClient:
    return TestClient(create_app(db))


@pytest.mark.parametrize("host_template, expected", ROWS)
def test_router_dispatch(client, published_id, token, host_template, expected):
    host = host_template.format(
        id=published_id, id_upper=published_id.upper(), short=published_id[:-1]
    )
    if expected == "gallery":
        # /healthz is a gallery-only route: it answers 200 only from the gallery app.
        response = client.get("/healthz", headers={"host": host})
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    elif expected == "content":
        # A framed GET of the tokenised path answers the real page only from the content app.
        response = client.get(f"/t/{token}/", headers={"host": host, **FRAMED})
        assert response.status_code == 200
        assert "hi" in response.text
    else:
        # Neither app is reached: the router answers its own 404 directly.
        response = client.get("/healthz", headers={"host": host})
        assert response.status_code == 404
        assert response.text == "not found"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-type"].startswith("text/plain")


def test_a_content_host_never_reaches_the_gallery_api(client, published_id):
    """The gallery's /api/v1/artifacts is key-gated and answers 401 to a caller with no
    key. A negative assertion here (`!= 401`) would let ANY other status through, a 500
    included -- assert what the content app actually answers: its own 404, with the base
    header set, for a route it does not have."""
    response = client.get(
        "/api/v1/artifacts", headers={"host": f"{published_id}.tenfourty.com"}
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp


@pytest.mark.asyncio
async def test_a_request_with_no_host_header_at_all_gets_the_router_404(db):
    """TestClient always sends a Host header, so the one client we cannot use to prove this
    row is TestClient itself -- drive the raw ASGI callable directly instead."""
    app = create_app(db)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/healthz",
        "raw_path": b"/healthz",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m["body"] for m in sent if m["type"] == "http.response.body")
    assert start["status"] == 404
    assert body == b"not found"
    assert dict(start["headers"])[b"x-content-type-options"] == b"nosniff"
