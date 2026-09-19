from fastapi.testclient import TestClient

from artifacts import store
from artifacts.app import create_gallery_app

XSS = '"><img src=x onerror=alert(1)>'


def test_the_list_escapes_a_hostile_title(db):
    store.publish(db, "ops-seat", XSS, "<!doctype html><html><head></head><body>x</body></html>")
    page = TestClient(create_gallery_app(db)).get("/", headers={"Remote-User": "jeremy"}).text
    assert "<img src=x" not in page
    assert "&lt;img src=x" in page or "&#" in page


def test_the_detail_page_frames_the_content_origin_and_inlines_nothing(db):
    published = store.publish(
        db, "ops-seat", "Board", "<!doctype html><html><head></head><body>secret-body</body></html>"
    )
    page = TestClient(create_gallery_app(db)).get(
        f"/a/{published.id}", headers={"Remote-User": "jeremy"}
    ).text
    token = store.read_token(db, published.id)
    assert f'src="{store.content_url(published.id, token)}"' in page
    assert "secret-body" not in page


def test_the_gallery_sets_its_own_strict_headers(db):
    response = TestClient(create_gallery_app(db)).get("/", headers={"Remote-User": "jeremy"})
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "frame-src https://*.tenfourty.com" in csp
    assert "frame-src https:;" not in csp


def test_an_unknown_artifact_gets_a_404_not_a_frame(db):
    """An unchecked id renders a frame pointing at an invented subdomain."""
    response = TestClient(create_gallery_app(db)).get(
        "/a/aaaaaaaaaaaaaaaaaaaaaaaaaa", headers={"Remote-User": "jeremy"}
    )
    assert response.status_code == 404
    assert "<iframe" not in response.text


def test_the_detail_page_escapes_the_title_it_now_shows(db):
    published = store.publish(
        db, "ops-seat", XSS, "<!doctype html><html><head></head><body>x</body></html>"
    )
    page = TestClient(create_gallery_app(db)).get(
        f"/a/{published.id}", headers={"Remote-User": "jeremy"}
    ).text
    assert "<img src=x" not in page
