import pytest
from fastapi.testclient import TestClient

from artifacts import keys, store
from artifacts.content import create_content_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"
GALLERY = "https://artifacts.tenfourty.site"
FRAMED = {"sec-fetch-dest": "iframe"}


@pytest.fixture()
def published(db):
    """Two versions, so v1 is genuinely an older one. A fixture that publishes
    once makes `/v/1` the CURRENT version, and a test called "an older version
    is inert" then proves nothing about older versions."""
    keys.mint(db, agent="ops-seat")
    artifact = store.publish(db, "ops-seat", "Board", PAGE)
    store.patch(db, "ops-seat", artifact.id, PAGE.replace("hi", "hi again"), artifact.revision)
    return artifact


@pytest.fixture()
def content(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    return client


def test_a_framed_request_gets_the_page(content):
    response = content.get("/", headers=FRAMED)
    assert response.status_code == 200
    assert "Board" in response.text


def test_a_top_level_request_is_sent_to_the_gallery_instead(content, published):
    response = content.get("/", headers={"sec-fetch-dest": "document"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"{GALLERY}/a/{published.id}"


def test_the_header_set_is_on_every_page_response(content):
    headers = content.get("/", headers=FRAMED).headers
    csp = headers["content-security-policy"]
    for directive in [
        "default-src 'none'",
        "connect-src 'self'",
        "form-action 'none'",
        "base-uri 'none'",
        "worker-src 'none'",
        f"frame-ancestors {GALLERY}",
    ]:
        assert directive in csp
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-dns-prefetch-control"] == "off"
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert headers["cross-origin-resource-policy"] == "same-origin"


def test_an_older_version_is_inert_but_still_answers_the_contract(content):
    """Inert is not the same as absent. A page written to the contract does
    `await window.claude.use("db")` and branches on null; strip window.claude
    and that page throws instead of degrading."""
    response = content.get("/v/1", headers=FRAMED)
    assert response.status_code == 200
    assert "hi</body>" in response.text and "hi again" not in response.text
    assert "connect-src 'none'" in response.headers["content-security-policy"]
    assert "window.claude" in response.text
    assert "use:async(n)=>null" in response.text


def test_the_current_version_by_number_is_inert_too(content):
    """Liveness comes from the route, not from the number. Asking for version 2
    explicitly asks for a snapshot, and a snapshot gets the inert runtime --
    otherwise the number the gallery happens to link becomes a privilege."""
    response = content.get("/v/2", headers=FRAMED)
    assert response.status_code == 200
    assert "hi again" in response.text
    assert "use:async(n)=>null" in response.text


def test_the_current_version_carries_the_runtime_shim(content):
    assert "window.claude" in content.get("/", headers=FRAMED).text


@pytest.mark.parametrize("sent", [{}, {"sec-fetch-dest": "empty"}, {"sec-fetch-dest": ""}])
def test_a_request_that_does_not_say_it_is_framed_never_gets_the_page(content, sent):
    """curl, a script's fetch and an old browser all send no Sec-Fetch-Dest.
    Denying only "document" would serve every one of them the page."""
    response = content.get("/", headers=sent, follow_redirects=False)
    assert response.status_code == 303
    assert "window.claude" not in response.text


def test_a_page_with_no_head_still_gets_the_runtime(db, monkeypatch):
    """`<!doctype html><body>...` is ordinary agent-written HTML. Injecting only
    at `</head>` leaves that page with no runtime and reports nothing."""
    keys.mint(db, agent="headless-seat")
    artifact = store.publish(db, "headless-seat", "Bare", "<!doctype html><body>hi</body>")
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{artifact.id}.tenfourty.com"})
    text = client.get("/", headers=FRAMED).text
    assert "window.claude" in text
    assert text.index("window.claude") < text.index("hi")


def test_the_gallery_origin_is_read_at_call_time(content, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://elsewhere.example")
    response = content.get("/", headers={"sec-fetch-dest": "document"}, follow_redirects=False)
    assert response.headers["location"] == f"https://elsewhere.example/a/{published.id}"


def test_an_unknown_artifact_host_is_not_found(content):
    content.headers.update({"host": "aaaaaaaaaaaaaaaaaaaaaaaaaa.tenfourty.com"})
    assert content.get("/", headers=FRAMED).status_code == 404


def test_a_pending_version_is_never_served(db, content, published):
    """Nothing in this arc writes a 'pending' version row, so this test writes one by
    hand. That is the point: the `status = 'live'` filter in _load has no other way to be
    exercised, and an unexercised filter is indistinguishable from an absent one. Without
    the assertion, the day pending-on-widening (design D11) lands, the content server
    starts serving bodies whose whole reason for existing is that they are NOT yet cleared
    to be served -- and every test here would still be green."""
    with db.connect() as conn:
        conn.execute(
            "insert into version (artifact_id, n, body, revision, status, created_by) "
            "values (%s, %s, %s, %s, 'pending', %s)",
            (published.id, 99, "<!doctype html><body>not cleared</body>", "r99", "ops-seat"),
        )
    assert content.get("/v/99", headers=FRAMED).status_code == 404
    # and the live current version is still served, so the filter has not just broken reads
    assert content.get("/", headers=FRAMED).status_code == 200
