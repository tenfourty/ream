"""The shared look, served as a real stylesheet.

homelab-ij8.21, Jeremy 19 Sep 2026: serve it instead of 150 lines pasted into
every page, while agents stay free to design a page however they like. It is
7.9 KB, which is 42% of the project-status template.

The interesting constraint is not the route, it is WHERE the route has to be.
A published page runs on its own origin under `style-src 'self'`, so a
stylesheet served from the gallery would be blocked -- silently, in exactly
the way this service exists to stop. It has to come from the page's own host.
"""

import pytest
from fastapi.testclient import TestClient

from artifacts import content, keys, store, templates
from artifacts.content import create_content_app

PAGE = "<!doctype html><html><head><title>Board</title></head><body>hi</body></html>"


@pytest.fixture()
def published(db):
    keys.mint(db, agent="ops-seat")
    return store.publish(db, "ops-seat", "Board", PAGE)


@pytest.fixture()
def client(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    c = TestClient(create_content_app(db))
    c.headers.update({"host": f"{published.id}.tenfourty.com"})
    return c


def test_the_stylesheet_is_served_from_the_pages_own_host(client):
    got = client.get("/_shared.css")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("text/css")
    assert ":root{" in got.text and "--accent:" in got.text


def test_it_needs_no_token_because_a_link_cannot_carry_one(client):
    """A <link> is fetched by the browser as a subresource, with no capability
    token and sec-fetch-dest: style. Token-only would mean no page could ever
    use it."""
    got = client.get("/_shared.css", headers={"sec-fetch-dest": "style"})
    assert got.status_code == 200


def test_it_is_not_swallowed_by_the_framed_only_redirect(client):
    """Every page route 303s a request that is not framed. A stylesheet never
    is, so this route must be its own -- and the failure would have been a
    redirect to the gallery served as CSS, which renders as no styling at all."""
    got = client.get("/_shared.css", follow_redirects=False)
    assert got.status_code == 200


def test_it_is_the_same_bytes_the_templates_are_built_from(client):
    """One source. `sync-templates.py` splices this text between each
    template's `shared:` sentinels, so a page that links the stylesheet and a
    page that inlines the block are looking at the same CSS."""
    on_disk = (templates.DIR / "_shared.css").read_text()
    assert client.get("/_shared.css").text == on_disk


def test_a_second_fetch_revalidates_to_304(client):
    first = client.get("/_shared.css")
    etag = first.headers["etag"]
    again = client.get("/_shared.css", headers={"if-none-match": etag})
    assert again.status_code == 304
    assert again.content == b""


def test_the_etag_follows_the_content(client, monkeypatch, tmp_path):
    """Content-derived, so a pod serving NEW css answers a stale conditional
    with 200 rather than an empty 304 that leaves a page unstyled. Proved by
    changing the file, not by asserting that an unchanged one is unchanged."""
    before = client.get("/_shared.css").headers["etag"]

    (tmp_path / "_shared.css").write_text(":root{--accent:rebeccapurple}\n")
    monkeypatch.setattr(templates, "DIR", tmp_path)
    templates.shared_css.cache_clear()
    try:
        after = client.get("/_shared.css")
        assert after.headers["etag"] != before, "new bytes, new validator"
        assert "rebeccapurple" in after.text
        stale = client.get("/_shared.css", headers={"if-none-match": before})
        assert stale.status_code == 200, (
            "a browser holding the old validator is given the new stylesheet, "
            "not an empty 304 that would leave the page unstyled"
        )
    finally:
        templates.shared_css.cache_clear()


def test_it_carries_the_content_origins_minimum_headers(client):
    got = client.get("/_shared.css")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors" in got.headers["content-security-policy"]


def test_a_page_that_links_it_is_not_a_csp_warning(db):
    """The link a page writes must not be reported as a remote stylesheet --
    it is same-origin, which is exactly what the policy permits."""
    from artifacts import csp

    body = (
        '<!doctype html><html><head><link rel="stylesheet" href="/_shared.css">'
        "</head><body>hi</body></html>"
    )
    assert csp.check(body) == []
    assert content.headers_for(True)["content-security-policy"].count(
        "style-src 'self'"
    ) == 1, "and the policy it relies on is still the one being served"
