from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from artifacts import frame_pass, keys, store
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


def _pass(db, artifact_id, version=None):
    return frame_pass.mint(db, artifact_id, version)


def test_the_page_is_served_only_with_a_pass_for_it(db, content, published):
    good = _pass(db, published.id)
    assert content.get(f"/p/{good}/", headers=FRAMED).status_code == 200
    assert content.get("/p/not-a-pass/", headers=FRAMED).status_code == 403
    assert content.get("/", headers=FRAMED).status_code == 404
    # a case-swap is a different pass, byte for byte -- no case-folding anywhere
    swapped = _pass(db, published.id)
    assert content.get(f"/p/{swapped.swapcase()}/", headers=FRAMED).status_code == 403
    # another page's pass, presented on this page's host, opens nothing...
    other = store.publish(db, "ops-seat", "Other", PAGE)
    theirs = _pass(db, other.id)
    assert content.get(f"/p/{theirs}/", headers=FRAMED).status_code == 403
    # ...and is NOT burned by the attempt: it still works for its own page
    assert frame_pass.spend(db, theirs, other.id) is not None


def test_a_pass_works_once(db, content, published):
    """homelab-ij8.101: a pass copied out of a page, or out of a history entry,
    is worthless once the frame has loaded -- the gap claude.ai's hour-long
    reusable pass leaves open."""
    once = _pass(db, published.id)
    assert content.get(f"/p/{once}/", headers=FRAMED).status_code == 200
    again = content.get(f"/p/{once}/", headers=FRAMED)
    assert again.status_code == 403
    assert ">hi<" not in again.text


def test_a_pass_expires_unused(db, content, published):
    stale = _pass(db, published.id)
    with db.connect() as conn:
        conn.execute(
            "update frame_pass set expires_at = %s",
            (datetime.now(UTC) - timedelta(seconds=1),),
        )
    assert content.get(f"/p/{stale}/", headers=FRAMED).status_code == 403


def test_the_lifetime_is_minutes_not_an_hour():
    assert 30 <= frame_pass.LIFETIME_SECONDS <= 300


def test_passes_are_stored_hashed(db, published):
    plain = _pass(db, published.id)
    with db.connect() as conn:
        stored = [r[0] for r in conn.execute("select token_sha256 from frame_pass")]
    assert plain not in stored and len(stored) == 1


def test_a_deleted_page_is_not_served_even_with_a_fresh_pass(db, content, published):
    fresh = _pass(db, published.id)
    with db.connect() as conn:
        conn.execute(
            "update artifact set deleted_at = %s where id = %s",
            (datetime.now(UTC), published.id),
        )
    assert content.get(f"/p/{fresh}/", headers=FRAMED).status_code == 404


def test_a_pass_can_name_a_version(db, content, published):
    store.patch(
        db, "ops-seat", published.id, PAGE.replace("hi", "hello"), published.revision
    )
    old = content.get(f"/p/{_pass(db, published.id, 1)}/", headers=FRAMED)
    live = content.get(f"/p/{_pass(db, published.id)}/", headers=FRAMED)
    assert ">hi<" in old.text and ">hello<" in live.text


def test_an_expired_frame_asks_the_gallery_for_a_new_pass_and_says_so_in_words(
    db, content, published
):
    page = content.get("/p/spent-or-unknown/", headers=FRAMED)
    assert page.status_code == 403
    assert "has expired" in page.text
    assert 'type:"artifact-expired"' in page.text
    # posted only to the gallery, never "*"
    assert '"*"' not in page.text
    assert page.headers["cache-control"] == "no-store"


def test_a_non_ascii_pass_is_refused_not_a_crash(content):
    assert content.get("/p/%C3%A9t%C3%A9/", headers=FRAMED).status_code == 403


def test_an_unframed_pass_is_redirected_and_not_spent(db, content, published):
    """Opened as a page of its own, a pass goes to the gallery -- and is not
    burned by a request that was never going to be shown the page."""
    unused = _pass(db, published.id)
    response = content.get(f"/p/{unused}/", follow_redirects=False)
    assert response.status_code == 303
    assert unused not in response.headers["location"]
    assert response.headers["location"].endswith(f"/a/{published.id}")
    assert content.get(f"/p/{unused}/", headers=FRAMED).status_code == 200


def test_the_permanent_token_no_longer_reads_the_page(db, content, published):
    token = store.read_token(db, published.id)
    for path in (f"/t/{token}/", f"/t/{token}/v/1", f"/p/{token}/"):
        response = content.get(path, headers=FRAMED)
        assert response.status_code in (403, 404), path
        assert ">hi<" not in response.text


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
        json={
            "body": PAGE.replace("hi", "hello"),
            "expected_revision": created.json()["revision"],
        },
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
    store.patch(
        db, "ops-seat", published.id, PAGE.replace("hi", "hello"), published.revision
    )
    assert store.read_token(db, published.id) == before


def test_every_artifact_gets_its_own_unguessable_token(db):
    tokens = {
        store.read_token(db, store.publish(db, "ops-seat", "t", PAGE).id)
        for _ in range(20)
    }
    assert len(tokens) == 20
    assert all(len(token) >= 32 for token in tokens)


def test_a_public_page_still_needs_a_pass_in_this_part(db, content, published):
    """Fail-closed by ruling: sharing is a deliberate per-page act that has not
    been built yet, so `visibility` opens nothing on its own."""
    with db.connect() as conn:
        conn.execute(
            "update artifact set visibility = 'public' where id = %s", (published.id,)
        )
    assert content.get("/", headers=FRAMED).status_code == 404
    assert (
        content.get(f"/p/{_pass(db, published.id)}/", headers=FRAMED).status_code == 200
    )


def test_content_url_is_what_the_gallery_frames(db, published, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    assert store.content_url(published.id, "PASS") == (
        f"https://{published.id}.tenfourty.com/p/PASS/"
    )


def _fingerprint(response):
    """(status, body, content-type, every header except Date) -- Date is the one header
    that legitimately differs between two responses taken a moment apart."""
    headers = {k: v for k, v in response.headers.items() if k.lower() != "date"}
    return response.status_code, response.content, headers


def test_a_wrong_pass_answers_exactly_like_a_missing_artifact(db, content, published):
    """Not just the same status: the SAME body, content-type and header set, on a
    wrong pass for a real page and on a real pass presented on a host with no page
    at all -- so nothing here can be used to tell the two apart."""
    wrong_on_existing = content.get("/p/not-a-pass/", headers=FRAMED)

    missing_host = TestClient(create_content_app(db))
    missing_host.headers.update({"host": "b" * 26 + ".tenfourty.com"})
    real_pass_on_missing_host = missing_host.get(
        f"/p/{_pass(db, published.id)}/", headers=FRAMED
    )

    assert _fingerprint(wrong_on_existing) == _fingerprint(real_pass_on_missing_host)


def test_no_trailing_slash_redirect_can_put_a_pass_on_the_wire(db, content, published):
    """Starlette's `redirect_slashes` (on by default) would answer this with a 307 back
    to the URL with the pass in it, BEFORE the framed-only check, and its Location
    scheme follows the ASGI scope -- behind a TLS-terminating ingress that can be
    `http://`, putting the pass on the wire in plaintext."""
    live = _pass(db, published.id)
    response = content.get(f"/p/{live}", headers=FRAMED, follow_redirects=False)
    assert response.status_code == 404
    assert "location" not in response.headers
    assert live not in response.text


def test_the_gallery_hands_out_a_fresh_pass_for_a_spent_frame(db, content, published):
    """homelab-ij8.101: gallery.js asks this when a frame reports its pass spent."""
    gallery = TestClient(create_gallery_app(db))
    first = gallery.get(f"/a/{published.id}/frame")
    second = gallery.get(f"/a/{published.id}/frame")
    assert first.status_code == second.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    a, b = first.json()["src"], second.json()["src"]
    assert a != b, "a new pass every time"
    assert a.startswith(f"https://{published.id}.tenfourty.com/p/")
    path = "/" + a.split("/", 3)[3]
    assert content.get(path, headers=FRAMED).status_code == 200


def test_no_pass_for_a_page_that_does_not_exist_or_was_deleted(db, published):
    gallery = TestClient(create_gallery_app(db))
    assert gallery.get(f"/a/{'b' * 26}/frame").status_code == 404
    store.set_deleted(db, published.id, True, agent="ops-seat")
    assert gallery.get(f"/a/{published.id}/frame").status_code == 404


def test_the_page_view_carrying_a_pass_is_never_cached(db, published):
    page = TestClient(create_gallery_app(db)).get(f"/a/{published.id}")
    assert page.headers["cache-control"] == "no-store"
