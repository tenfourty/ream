import re

import pytest
from fastapi.testclient import TestClient

from artifacts import store
from artifacts.app import create_gallery_app

XSS = '"><img src=x onerror=alert(1)>'

_PAGE = "<!doctype html><html><head></head><body>x</body></html>"


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


@pytest.mark.parametrize("which", ["list", "detail"])
def test_the_gallery_sets_its_own_strict_headers(db, which):
    """Both routes must carry the same strict header set -- `/a/{id}` is the one page that
    ever holds the capability token, so it is checked exactly as strictly as `/`, not on the
    strength of `/` alone."""
    published = store.publish(db, "ops-seat", "Board", _PAGE)
    path = "/" if which == "list" else f"/a/{published.id}"
    response = TestClient(create_gallery_app(db)).get(path, headers={"Remote-User": "jeremy"})
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "frame-src https://*.tenfourty.com" in csp
    assert "frame-src https:;" not in csp
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"


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


def test_the_detail_iframe_is_sandboxed_against_navigation_and_forms(db):
    """No allow-top-navigation*, no allow-forms, no allow-modals: an agent's page must never
    be able to navigate Jeremy's signed-in gallery tab, e.g. to a fake sign-in page. Asserting
    set equality (not just "contains") means adding a token back later fails this test too."""
    published = store.publish(db, "ops-seat", "Board", _PAGE)
    page = TestClient(create_gallery_app(db)).get(
        f"/a/{published.id}", headers={"Remote-User": "jeremy"}
    ).text
    match = re.search(r'<iframe[^>]*\bsandbox="([^"]*)"', page)
    assert match is not None, "detail page has no sandboxed iframe"
    assert set(match.group(1).split()) == {
        "allow-scripts",
        "allow-same-origin",
        "allow-popups",
        "allow-popups-to-escape-sandbox",
    }


def _ids_in_order(page: str) -> list[str]:
    return re.findall(r'href="/a/([a-z2-7]{26})"', page)


def test_the_list_is_capped_at_two_hundred_rows(db):
    for i in range(201):
        store.publish(db, "ops-seat", f"page {i}", _PAGE)
    page = TestClient(create_gallery_app(db)).get("/", headers={"Remote-User": "jeremy"}).text
    assert len(_ids_in_order(page)) == 200


def test_the_list_orders_newest_first(db):
    older = store.publish(db, "ops-seat", "older", _PAGE)
    with db.connect() as conn:
        conn.execute(
            "update artifact set updated_at = now() - interval '1 hour' where id = %s",
            (older.id,),
        )
    newer = store.publish(db, "ops-seat", "newer", _PAGE)
    page = TestClient(create_gallery_app(db)).get("/", headers={"Remote-User": "jeremy"}).text
    order = _ids_in_order(page)
    assert order.index(newer.id) < order.index(older.id)


def test_the_list_includes_every_agents_pages(db):
    """The gallery is Jeremy's view of everything -- unlike the agent API, its query carries
    no `where agent = ...`."""
    one = store.publish(db, "ops-seat", "one", _PAGE)
    two = store.publish(db, "other-seat", "two", _PAGE)
    page = TestClient(create_gallery_app(db)).get("/", headers={"Remote-User": "jeremy"}).text
    ids = _ids_in_order(page)
    assert one.id in ids
    assert two.id in ids


def test_the_list_breaks_updated_at_ties_by_id(db):
    """Every row with the identical updated_at must still come back in one fixed order, not
    whatever a `select` with no full ORDER BY happens to return today. Ten rows, not two: with
    only two, insertion order coincidentally matches id-desc about half the time (measured: a
    mutant that removes `, id desc` was only caught 4 times in 12 runs), so a two-row sample
    gives almost no assurance the ORDER BY clause is doing anything. Ten rows landing in
    id-desc order by chance alone is astronomically unlikely."""
    ids = [store.publish(db, "ops-seat", f"page {i}", _PAGE).id for i in range(10)]
    with db.connect() as conn:
        conn.execute(
            "update artifact set updated_at = '2026-01-01T00:00:00Z' where id = any(%s)",
            (ids,),
        )
    page = TestClient(create_gallery_app(db)).get("/", headers={"Remote-User": "jeremy"}).text
    order = [i for i in _ids_in_order(page) if i in ids]
    assert order == sorted(ids, reverse=True)


def test_store_module_docstring_is_encodable():
    """A literal `\\ud800` escape in a non-raw docstring becomes a real lone surrogate in
    `__doc__`; harmless import on 3.12, but a bare `import artifacts.store` crashes on 3.14."""
    import artifacts.store as store_module

    store_module.__doc__.encode("utf-8")
