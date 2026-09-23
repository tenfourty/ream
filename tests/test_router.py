"""The router between the gallery host and the content hosts is the only code
between a content-domain request -- which never passes through Authelia -- and
the gallery app, which has no in-app identity check of its own (Authelia gates
it at ITS ingress). A router that fails open on anything it does not recognise
turns that boundary into a boundary in name only, so every row here asserts
WHICH app answered, not just a status code.
"""

import json

import pytest
from fastapi.testclient import TestClient

from artifacts import frame_pass, keys, store
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
    # A pass, good once (homelab-ij8.101); each test gets its own.
    return frame_pass.mint(db, published_id)


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
        response = client.get(f"/p/{token}/", headers={"host": host, **FRAMED})
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


async def _raw_get(app, headers: list[tuple[bytes, bytes]], path: str = "/healthz"):
    """Drive the raw ASGI callable directly with an explicit header list, so a header key
    (like `host`) can appear more than once -- something no dict of headers can do."""
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
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m["body"] for m in sent if m["type"] == "http.response.body")
    return start["status"], body


@pytest.mark.asyncio
async def test_duplicate_host_headers_are_resolved_the_same_way_by_router_and_content(
    db, published_id, token
):
    """The router (`app.py`) and the content app (`content.py`) must pick the SAME host
    when a request carries two Host headers -- both read the FIRST, via the shared
    `hosts.first_host_header`. If they ever disagreed (router picks one, content the
    other), the router could hand a request to the wrong app, or the content app could
    look up the wrong artifact under the router's nose."""
    app = create_app(db)
    content_host = f"{published_id}.tenfourty.com".encode()
    gallery_host = GALLERY_HOST.encode()
    framed = [(b"sec-fetch-dest", b"iframe")]

    # content host FIRST -> the content app answers the tokenised path
    status, body = await _raw_get(
        app, [(b"host", content_host), (b"host", gallery_host), *framed], f"/p/{token}/"
    )
    assert status == 200
    assert b"hi" in body

    # gallery host FIRST -> the gallery app answers /healthz
    status, body = await _raw_get(
        app, [(b"host", gallery_host), (b"host", content_host)], "/healthz"
    )
    assert status == 200
    assert json.loads(body) == {"status": "ok"}


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


# --- homelab-ij8.4: a caller's own key names must not come back in a 422 ------


def test_a_caller_chosen_key_name_never_returns_in_a_validation_error(db):
    """Written as a forward-looking note on 19 Sep: the validation handler
    returns {loc, type}, which is safe only while no request model has a
    dict-typed or aliased field. Two now do -- CommentBody.anchor is
    `dict | None` and DecisionBody.options is `Any` -- so the condition the
    bead was waiting for has been met and this is the test it asked for.

    WHY IT MATTERS: `loc` on a dict field is the caller's OWN key, and the
    error lands in an agent's transcript. A field named after something the
    caller would rather not repeat is then repeated, by us, in a durable log.
    """
    import os

    from fastapi.testclient import TestClient

    from artifacts import keys, store
    from artifacts.app import create_gallery_app

    os.environ["ARTIFACTS_CONTENT_DOMAIN"] = "tenfourty.com"
    token, _ = keys.mint(db, agent="seat")
    page = store.publish(
        db, "seat", "B",
        "<!doctype html><html><head><title>t</title></head><body><p>x</p></body></html>",
    )
    client = TestClient(create_gallery_app(db=db))
    headers = {"Authorization": f"Bearer {token}"}
    # A key name that is unmistakable if it ever comes back.
    caller_key = "A_NAME_THE_CALLER_WOULD_NOT_WANT_REPEATED"

    attempts = [
        (f"/api/v1/artifacts/{page.id}/comments", {"body": "x", "anchor": {caller_key: 1}}),
        (f"/api/v1/artifacts/{page.id}/comments", {"body": "x", "anchor": "not-a-dict"}),
        (
            f"/api/v1/artifacts/{page.id}/decisions",
            {"question": "q", "options": {caller_key: 1}},
        ),
    ]
    for path, payload in attempts:
        got = client.post(path, json=payload, headers=headers)
        assert got.status_code == 422, (path, got.status_code)
        assert caller_key not in got.text, (
            f"{path} handed the caller's own key name back in a 422"
        )


# --- the face an agent outside the house arrives at --------------------------
#
# Jeremy, 22 Sep 2026, making external access the priority: "ideally to onboard
# an agent I want to give it a url to you and it will get everything it needs
# and then ask me for an access key for it's machine".
#
# The door is a Cloudflare Tunnel route, which points a public hostname at
# something in the cluster. Point it at the gallery's hostname and his wall is
# on the internet behind whatever that route happens to carry -- Authelia is
# applied by the INTERNAL ingress's annotations, and a new route need not repeat
# them. So the public face is its own name, and the app, not an ingress, decides
# what lives there.

API_HOST = "agents.tenfourty.site"


@pytest.fixture()
def api_host(monkeypatch):
    monkeypatch.setenv("ARTIFACTS_API_HOST", API_HOST)
    return API_HOST


def test_the_api_host_serves_the_agent_api(client, api_host, db):
    token, _ = keys.mint(db, agent="outside-seat")
    response = client.get(
        "/api/v1/artifacts",
        headers={"host": api_host, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


def test_the_api_host_does_not_serve_the_gallery(client, api_host, published_id):
    """The one that matters. His wall must not exist on the public name, whatever
    an ingress is or is not carrying."""
    for path in ("/", f"/a/{published_id}", f"/d/{published_id}/answer", "/gallery.js"):
        response = client.get(path, headers={"host": api_host})
        assert response.status_code == 404, f"{path} answered {response.status_code}"


def test_the_gallery_host_still_serves_the_gallery_when_an_api_host_exists(
    client, api_host
):
    assert client.get("/healthz", headers={"host": GALLERY_HOST}).status_code == 200
    assert client.get("/", headers={"host": GALLERY_HOST}).status_code == 200


def test_the_same_name_for_both_resolves_to_the_gallery(client, monkeypatch, db):
    """If the two are ever configured identically by mistake, the request must go
    to the app that sits BEHIND Authelia, not the one that does not."""
    monkeypatch.setenv("ARTIFACTS_API_HOST", GALLERY_HOST)
    assert client.get("/", headers={"host": GALLERY_HOST}).status_code == 200


def test_no_api_host_configured_leaves_every_other_name_404(client, monkeypatch):
    monkeypatch.delenv("ARTIFACTS_API_HOST", raising=False)
    response = client.get("/healthz", headers={"host": API_HOST})
    assert response.status_code == 404
    assert response.text == "not found"


def test_the_onboarding_document_is_open_and_names_the_address_it_was_asked_on(
    client, api_host
):
    """The one unauthenticated document on the public face. It must work with no
    key at all -- that is its whole job -- and it must point at the address the
    caller already found, not at one out of a config file that may only resolve
    inside the house."""
    response = client.get("/agents.txt", headers={"host": api_host})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert f"://{api_host}/api/v1" in body, "the API base, at the name it was asked on"
    assert "artifacts.tenfourty.site" not in body, "and never the internal name"
    assert "ARTIFACTS_SESSION" in body, "how to name yourself once you have a key"
    # how to get a key: Jeremy approves a code; no key is ever handed out here
    # (homelab-ij8.102 replaced "ask him for one by hand")
    assert "artifacts-connect" in body and "approves" in body


def test_the_onboarding_document_carries_no_key_and_no_secret(client, api_host, db):
    keys.mint(db, agent="outside-seat")
    body = client.get("/agents.txt", headers={"host": api_host}).text
    assert "art_" not in body.replace("`art_`", ""), "no token, not even an example one"
    assert "Bearer " not in body


def test_an_agent_on_the_lan_can_read_the_onboarding_document_without_a_key(client):
    """The gallery's hostname is behind Authelia for everything except /api/v1,
    and an agent holds no Authelia session and never will. So the one prefix it
    can reach has to carry the document too, or the only agents that can read
    how to join are the ones that have already joined."""
    response = client.get("/api/v1/agents.txt", headers={"host": GALLERY_HOST})
    assert response.status_code == 200, "no key, and it must still answer"
    assert "artifacts" in response.text
    assert f"://{GALLERY_HOST}/api/v1" in response.text


def test_the_onboarding_document_never_tells_a_stranger_to_use_plain_http(
    client, api_host
):
    """Caught on the running pod rather than here, and it would have shipped.

    The scheme comes from the connection, and this app sits behind a
    TLS-terminating ingress that speaks plain HTTP to it — so the document
    advertised `http://` for a real hostname. An onboarding document is the one
    place that must never say that to a stranger: an agent following it
    literally puts its key on the wire in clear.
    """
    body = client.get("/agents.txt", headers={"host": api_host}).text
    assert f"https://{api_host}/api/v1" in body
    assert "http://" not in body, "not once, anywhere in it"

    # The proxy's own word wins when it has one, because that is the scheme the
    # CALLER used, not the one the ingress used to reach us.
    forwarded = client.get(
        "/agents.txt", headers={"host": api_host, "x-forwarded-proto": "https"}
    ).text
    assert f"https://{api_host}" in forwarded


def test_a_loopback_caller_is_told_the_truth(client, monkeypatch):
    """http is both true and harmless for localhost, and pretending otherwise
    would send someone developing against it at a port that is not listening."""
    monkeypatch.setenv("ARTIFACTS_API_HOST", "localhost")
    body = client.get("/agents.txt", headers={"host": "localhost:8000"}).text
    assert "http://localhost:8000/api/v1" in body
