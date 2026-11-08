import json

import pytest
from fastapi.testclient import TestClient

from ream import frame_pass, keys, store
from ream.content import SHIM, create_content_app, inject

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
    store.patch(
        db, "ops-seat", artifact.id, PAGE.replace("hi", "hi again"), artifact.revision
    )
    return artifact


@pytest.fixture()
def content(db, published, monkeypatch):
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{published.id}.tenfourty.com"})
    return client


@pytest.fixture()
def token(db, published):
    """A fresh path each call: a pass is good once (homelab-ij8.101), so a test
    that loads the page twice needs two of them. `token(n)` reads version n."""

    def at(n=None):
        return f"/p/{frame_pass.mint(db, published.id, n)}/"

    return at


def test_a_framed_request_gets_the_page(content, token):
    response = content.get(token(), headers=FRAMED)
    assert response.status_code == 200
    assert "Board" in response.text


def test_a_top_level_request_is_sent_to_the_gallery_instead(content, published):
    response = content.get(
        "/", headers={"sec-fetch-dest": "document"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"{GALLERY}/a/{published.id}"


def test_the_header_set_is_on_every_page_response(content, token):
    """Checked on the current page AND on an inert version (`/v/1`): a header the live
    page happens to carry proves nothing about what an inert snapshot carries, and vice
    versa. `connect-src` differs by design (live vs inert), so it is parametrised; every
    other directive and header applies identically to both."""
    for path, connect in (
        (token(), "connect-src 'self'"),
        (token(1), "connect-src 'none'"),
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
        assert (
            headers["permissions-policy"]
            == "geolocation=(), camera=(), microphone=(), payment=()"
        )
        assert headers["cache-control"] == "no-store"


def test_an_older_version_is_inert_but_still_answers_the_contract(content, token):
    """Inert is not the same as absent. A page written to the contract does
    `await window.claude.use("db")` and branches on null; strip window.claude
    and that page throws instead of degrading."""
    response = content.get(token(1), headers=FRAMED)
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
    response = content.get(token(2), headers=FRAMED)
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
    response = content.get(token(), headers=FRAMED)
    assert "window.claude" in response.text
    assert "window.storage=null" not in response.text
    csp = response.headers["content-security-policy"]
    assert "connect-src 'self'" in csp
    assert "connect-src 'none'" not in csp


# ---------------------------------------------------------------------------
# The viewer report: height and decision-slot geometry, posted to the gallery.
# ---------------------------------------------------------------------------


def test_the_live_page_reports_height_and_decision_slots_to_the_gallery(content, token):
    """Only the LIVE route gets the viewer half of the shim -- the three triggers the
    design calls for (load, resize, mutation via ResizeObserver -- never a poll), and
    the one attribute a page marks a decision slot with."""
    text = content.get(token(), headers=FRAMED).text
    assert "data-artifact-decision" in text
    assert "ResizeObserver" in text
    assert "setInterval" not in text  # never a poll
    assert 'addEventListener("load"' in text
    assert 'addEventListener("resize"' in text
    assert "postMessage" in text
    assert "getBoundingClientRect" in text
    # whether the page is wider than its frame, so the gallery leaves such a frame
    # able to scroll sideways (runcove-5t3oj.62)
    assert "wide:" in text and "d.scrollWidth>d.clientWidth" in text


def test_the_viewer_report_posts_only_to_the_gallery_origin_never_to_star(
    content, token
):
    text = content.get(token(), headers=FRAMED).text
    assert "postMessage" in text
    assert f",{json.dumps(store.gallery_origin())})" in text
    assert '"*")' not in text


def test_the_viewer_report_origin_is_read_at_call_time(content, token, monkeypatch):
    """Exactly like headers_for and the redirect target: a module-level constant
    computed once at import would never see this monkeypatch."""
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://elsewhere.example")
    text = content.get(token(), headers=FRAMED).text
    assert f",{json.dumps('https://elsewhere.example')})" in text
    assert f",{json.dumps(GALLERY)})" not in text


def test_an_older_version_carries_no_viewer_report(content, token):
    """Inert means read-only, not read-only except for a viewer channel: an old
    snapshot is never framed by the gallery today, and must not start reporting
    geometry back to it."""
    for path in (token(1), token(2)):
        text = content.get(path, headers=FRAMED).text
        assert "ResizeObserver" not in text
        assert "data-artifact-decision" not in text
        assert "postMessage" not in text


@pytest.mark.parametrize(
    "sent", [{}, {"sec-fetch-dest": "empty"}, {"sec-fetch-dest": ""}]
)
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
    artifact = store.publish(
        db, "headless-seat", "Bare", "<!doctype html><body>hi</body>"
    )
    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(create_content_app(db))
    client.headers.update({"host": f"{artifact.id}.tenfourty.com"})
    text = client.get(f"/p/{frame_pass.mint(db, artifact.id)}/", headers=FRAMED).text
    assert "window.claude" in text
    assert text.index("window.claude") < text.index("hi")


def test_the_gallery_origin_is_read_at_call_time(content, published, monkeypatch):
    monkeypatch.setenv("REAM_GALLERY_ORIGIN", "https://elsewhere.example")
    response = content.get(
        "/", headers={"sec-fetch-dest": "document"}, follow_redirects=False
    )
    assert response.headers["location"] == f"https://elsewhere.example/a/{published.id}"


def test_an_unknown_artifact_host_is_not_found(content, token):
    """The fixture's REAL token, on a host with no matching artifact: this pins "a valid
    token on a missing host is 404", not just "bare / with no token is 404" -- which
    cannot tell a missing artifact apart from a missing token at all."""
    content.headers.update({"host": "aaaaaaaaaaaaaaaaaaaaaaaaaa.tenfourty.com"})
    response = content.get(token(), headers=FRAMED)
    assert response.status_code == 403 and "has expired" in response.text


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
            (
                published.id,
                99,
                "<!doctype html><body>not cleared</body>",
                "r99",
                "ops-seat",
            ),
        )
    assert content.get(token(99), headers=FRAMED).status_code == 404
    # and the live current version is still served, so the filter has not just broken reads
    assert content.get(token(), headers=FRAMED).status_code == 200


def test_every_content_response_carries_the_base_headers(content, token):
    """The global constraint is "every content response", not "every page response":
    the 303, FastAPI's own route-404 (no such route at all), the app's own 404 for a
    right-pass-wrong-version, and the expired page for a wrong pass all count. Each must
    carry nosniff and a CSP, and none may echo a value the request carried. (There
    was a 422 here while a version number was a path parameter; since passes name
    their own version, homelab-ij8.101, no content route takes one.)"""
    responses = [
        content.get(
            "/", headers={"sec-fetch-dest": "document"}, follow_redirects=False
        ),
        content.get(
            "/v/999999", headers=FRAMED
        ),  # no such route any more -> FastAPI's 404
        content.get(token(999999), headers=FRAMED),  # right token, no such version
        content.get("/p/not-a-pass/", headers=FRAMED),  # wrong pass
        content.get("/p/x/<script>alert(1)<", headers=FRAMED),  # no such route
    ]
    for response in responses:
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" in response.headers
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert f"frame-ancestors {GALLERY}" in csp
    assert responses[-1].status_code == 404
    assert "<script>" not in responses[-1].text
    assert "alert(1)" not in responses[-1].text


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

    monkeypatch.setenv("REAM_CONTENT_DOMAIN", "tenfourty.com")
    client = TestClient(
        create_content_app(ExplodingDB()), raise_server_exceptions=False
    )
    client.headers.update({"host": "aaaaaaaaaaaaaaaaaaaaaaaaaa.tenfourty.com"})
    response = client.get("/p/whatever/", headers=FRAMED)
    assert response.status_code == 500
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert f"frame-ancestors {GALLERY}" in csp


def test_inject_places_the_shim_right_after_the_leading_doctype():
    page = "<!doctype html><html><head><title>t</title></head><body>hi</body></html>"
    result = inject(page, SHIM)
    assert (
        result
        == "<!doctype html>"
        + SHIM
        + "<html><head><title>t</title></head><body>hi</body></html>"
    )


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


@pytest.mark.parametrize(
    "page",
    [
        # <!--> : abrupt closing of an EMPTY comment (WHATWG "abrupt-closing-of-empty-comment")
        "<!--><script>early()</script><!-- --><!doctype html><html><head></head><body></body></html>",
        # <!---> : same, one more dash
        "<!---><script>early()</script><!-- --><!doctype html><html><head></head><body></body></html>",
        # --!> : incorrectly closed comment (WHATWG "incorrectly-closed-comment")
        "<!-- a --!><script>early()</script><!-- b --><!doctype html><html><head></head><body></body></html>",
        # --!> ending appearing inside a script's own text
        "<!-- a --!><script>/* --><!doctype html> */ early()</script>",
        # <!--> ending appearing inside a script's own text
        "<!--><script>/* --><!doctype html> */ early()</script>",
    ],
)
def test_inject_recognises_every_html_comment_ending_not_just_dash_dash_gt(page):
    """The WHATWG HTML tokenizer ends a comment at `<!-->`/`<!--->` (abrupt closing of an
    empty comment) and at `--!>` (incorrectly closed comment), not just at `-->`. Missing
    those lets a comment appear to "end" only at the next real `-->`, well past the page's
    own `<script>` -- landing the shim after that script, or inside a script whose own
    text happens to contain one of these sequences. With all three endings recognised,
    none of these five pages looks like "comments then a leading doctype" any more, so the
    shim goes to the very start -- before the script tag either way, and the original
    page (script included) is served byte-for-byte after it."""
    result = inject(page, SHIM)
    assert result.index(SHIM) == 0
    assert result.index(SHIM) < result.index("early(")
    assert page in result


def test_inject_still_recognises_an_empty_comment_before_the_doctype():
    """`<!---->` (an opening `<!--` immediately followed by a closing `-->`, with nothing
    in between) is a real, complete, legitimate comment -- not one of the abrupt-closing
    forms above -- and must still be skipped over, landing the shim right after the
    doctype that follows it."""
    page = "<!----><!doctype html><html></html>"
    result = inject(page, SHIM)
    assert result == "<!----><!doctype html>" + SHIM + "<html></html>"


def test_inject_still_recognises_a_comment_containing_a_lone_double_dash():
    """A lone `--` inside a comment's text (not immediately followed by `>` or `!>`) does
    not end it -- the comment is still one real comment, ending at its own `-->`, and must
    still be skipped over."""
    page = "<!-- a -- b --><!doctype html><html></html>"
    result = inject(page, SHIM)
    assert result == "<!-- a -- b --><!doctype html>" + SHIM + "<html></html>"
