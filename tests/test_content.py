import pytest
from fastapi.testclient import TestClient

from artifacts import keys, store
from artifacts.content import SHIM, create_content_app, inject

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


@pytest.fixture()
def token(db, published):
    return store.read_token(db, published.id)


def test_a_framed_request_gets_the_page(content, token):
    response = content.get(f"/t/{token}/", headers=FRAMED)
    assert response.status_code == 200
    assert "Board" in response.text


def test_a_top_level_request_is_sent_to_the_gallery_instead(content, published):
    response = content.get("/", headers={"sec-fetch-dest": "document"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"{GALLERY}/a/{published.id}"


def test_the_header_set_is_on_every_page_response(content, token):
    """Checked on the current page AND on an inert version (`/v/1`): a header the live
    page happens to carry proves nothing about what an inert snapshot carries, and vice
    versa. `connect-src` differs by design (live vs inert), so it is parametrised; every
    other directive and header applies identically to both."""
    for path, connect in (
        (f"/t/{token}/", "connect-src 'self'"),
        (f"/t/{token}/v/1", "connect-src 'none'"),
    ):
        headers = content.get(path, headers=FRAMED).headers
        csp = headers["content-security-policy"]
        for directive in [
            "default-src 'none'",
            connect,
            "form-action 'none'",
            "base-uri 'none'",
            "worker-src 'none'",
            "webrtc 'block'",
            f"frame-ancestors {GALLERY}",
        ]:
            assert directive in csp
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["x-dns-prefetch-control"] == "off"
        assert headers["cross-origin-opener-policy"] == "same-origin"
        assert headers["cross-origin-resource-policy"] == "same-origin"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["permissions-policy"] == "geolocation=(), camera=(), microphone=(), payment=()"
        assert headers["cache-control"] == "no-store"


def test_an_older_version_is_inert_but_still_answers_the_contract(content, token):
    """Inert is not the same as absent. A page written to the contract does
    `await window.claude.use("db")` and branches on null; strip window.claude
    and that page throws instead of degrading."""
    response = content.get(f"/t/{token}/v/1", headers=FRAMED)
    assert response.status_code == 200
    assert "hi</body>" in response.text and "hi again" not in response.text
    csp = response.headers["content-security-policy"]
    assert "connect-src 'none'" in csp
    assert "connect-src 'self'" not in csp
    assert "window.claude" in response.text
    assert "use:async(n)=>null" in response.text
    assert "window.storage=null" in response.text


def test_the_current_version_by_number_is_inert_too(content, token):
    """Liveness comes from the route, not from the number. Asking for version 2
    explicitly asks for a snapshot, and a snapshot gets the inert runtime --
    otherwise the number the gallery happens to link becomes a privilege."""
    response = content.get(f"/t/{token}/v/2", headers=FRAMED)
    assert response.status_code == 200
    assert "hi again" in response.text
    csp = response.headers["content-security-policy"]
    assert "connect-src 'none'" in csp
    assert "connect-src 'self'" not in csp
    assert "use:async(n)=>null" in response.text
    assert "window.storage=null" in response.text


def test_the_current_version_carries_the_runtime_shim(content, token):
    """The reverse of the two inert tests above: the live page has egress open and no
    inert marker, so a mutant that served the inert shim (or inert headers) everywhere
    is caught here just as the inert tests catch the opposite mutant."""
    response = content.get(f"/t/{token}/", headers=FRAMED)
    assert "window.claude" in response.text
    assert "window.storage=null" not in response.text
    csp = response.headers["content-security-policy"]
    assert "connect-src 'self'" in csp
    assert "connect-src 'none'" not in csp


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
    token = store.read_token(db, artifact.id)
    text = client.get(f"/t/{token}/", headers=FRAMED).text
    assert "window.claude" in text
    assert text.index("window.claude") < text.index("hi")


def test_the_gallery_origin_is_read_at_call_time(content, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_GALLERY_ORIGIN", "https://elsewhere.example")
    response = content.get("/", headers={"sec-fetch-dest": "document"}, follow_redirects=False)
    assert response.headers["location"] == f"https://elsewhere.example/a/{published.id}"


def test_an_unknown_artifact_host_is_not_found(content):
    content.headers.update({"host": "aaaaaaaaaaaaaaaaaaaaaaaaaa.tenfourty.com"})
    assert content.get("/", headers=FRAMED).status_code == 404


def test_a_pending_version_is_never_served(db, content, published, token):
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
    assert content.get(f"/t/{token}/v/99", headers=FRAMED).status_code == 404
    # and the live current version is still served, so the filter has not just broken reads
    assert content.get(f"/t/{token}/", headers=FRAMED).status_code == 200


def test_every_content_response_carries_the_base_headers(content, token):
    """The global constraint is "every content response", not "every page response":
    the 303, the router's own 404s, and FastAPI's 404/422 all count. Each must carry
    nosniff and a CSP, and the 422 must never echo the value that triggered it."""
    responses = [
        content.get("/", headers={"sec-fetch-dest": "document"}, follow_redirects=False),
        content.get("/v/999999", headers=FRAMED),  # no such route any more -> 404
        content.get(f"/t/{token}/v/<script>alert(1)<", headers=FRAMED),  # not an int -> 422
    ]
    for response in responses:
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" in response.headers
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert f"frame-ancestors {GALLERY}" in csp
    assert responses[2].status_code == 422
    assert "<script>" not in responses[2].text
    assert "alert(1)" not in responses[2].text


def test_the_openapi_document_is_not_served(content):
    assert content.get("/openapi.json", headers=FRAMED).status_code == 404


def test_an_unhandled_error_still_carries_the_base_headers(monkeypatch):
    """Starlette's ServerErrorMiddleware sits OUTSIDE `_base_headers` -- a response it
    sends never passes through that middleware, so the exception handler itself must set
    the minimum header set directly, or a 500 would be the one content response with no
    CSP at all."""

    class ExplodingDB:
        def connect(self):
            raise RuntimeError("boom")

    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(ExplodingDB()), raise_server_exceptions=False)
    client.headers.update({"host": "aaaaaaaaaaaaaaaaaaaaaaaaaa.tenfourty.com"})
    response = client.get("/t/whatever/", headers=FRAMED)
    assert response.status_code == 500
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in response.headers


def test_inject_places_the_shim_right_after_the_leading_doctype():
    page = "<!doctype html><html><head><title>t</title></head><body>hi</body></html>"
    result = inject(page, SHIM)
    assert result == "<!doctype html>" + SHIM + "<html><head><title>t</title></head><body>hi</body></html>"


def test_inject_is_not_fooled_by_a_head_close_hidden_in_a_comment():
    page = (
        "<!doctype html><html><head><!-- </head> --><title>t</title></head>"
        "<body>hi</body></html>"
    )
    result = inject(page, SHIM)
    # the shim landed right after the doctype, well before the comment -- not inside it
    assert result.index(SHIM) < result.index("<!-- </head> -->")


def test_inject_leaves_a_script_containing_the_text_head_close_intact():
    page = (
        '<!doctype html><html><head><script>var x = "</head>";</script></head>'
        "<body>hi</body></html>"
    )
    result = inject(page, SHIM)
    assert '<script>var x = "</head>";</script>' in result


def test_inject_precedes_the_pages_first_script():
    page = '<!doctype html><html><head><script>console.log("hi")</script></head></html>'
    result = inject(page, SHIM)
    assert result.index(SHIM) < result.index("<script>console.log")


def test_inject_a_lazy_comment_cannot_swallow_a_script_before_a_later_doctype():
    """A lazy `.*?` comment (with DOTALL) can, under backtracking, span from the first
    `<!--` all the way to the LAST `-->` before a later doctype -- treating a genuine
    `<script>` tag in between as if it were commented out, and landing the shim after it."""
    page = "<!-- a --><script>early()</script><!-- b --><!doctype html><html></html>"
    result = inject(page, SHIM)
    # no comment can span a real `-->`, so this no longer looks like "comments then a
    # leading doctype" at all -- the shim lands at the very start, before everything.
    assert result.index(SHIM) == 0
    assert result.index(SHIM) < result.index("<script>early()")


def test_inject_leaves_a_script_containing_a_fake_arrow_intact():
    """The script's own text contains `--><!doctype html>`, which must not be read as a
    real comment-close-then-doctype -- the script must be served byte-for-byte, and the
    shim must not land inside it."""
    page = "<!-- a --><script>/* --><!doctype html> */ early()</script>"
    result = inject(page, SHIM)
    assert result.index(SHIM) == 0
    assert "<script>/* --><!doctype html> */ early()</script>" in result
