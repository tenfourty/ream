"""The wall holds 200 pages at a time; "Older pages" shows the next 200 in the same view."""

import re
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from artifacts import decisions, store
from artifacts.app import create_gallery_app

PAGE = (
    "<!doctype html><html><head><title>t</title></head><body><h1>Hi</h1></body></html>"
)
N = 205


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CONTENT_DOMAIN", "tenfourty.com")
    return TestClient(create_gallery_app(db=db))


def put(db, agent, title, **kw):
    return store.publish(db, agent, title, PAGE, **kw)


def ids(html):
    return re.findall(r'href="/a/([^"/#]+)"', html)


def older_href(html):
    found = re.findall(r'<a href="([^"]+)" rel="next">Older pages</a>', html)
    return found[0].replace("&amp;", "&") if found else None


def walk(client, url):
    """Every page of a view, following the link; returns the pages' ids per request."""
    seen = []
    while url:
        html = client.get(url).text
        seen.append(ids(html))
        url = older_href(html)
    return seen


def test_no_link_when_everything_fits(client, db):
    for i in range(3):
        put(db, "bosun", f"P{i}")
    assert older_href(client.get("/").text) is None


def test_exactly_a_page_has_no_link(client, db):
    for i in range(200):
        put(db, "bosun", f"P{i}")
    html = client.get("/").text
    assert len(ids(html)) == 200 and older_href(html) is None


def test_more_than_a_page_links_to_the_rest_without_repeats(client, db):
    made = [put(db, "bosun", f"P{i}").id for i in range(N)]
    pages = walk(client, "/")
    assert [len(p) for p in pages] == [200, N - 200]
    flat = [i for p in pages for i in p]
    assert len(set(flat)) == N and set(flat) == set(made)


def test_pinned_and_waiting_stay_first_and_paging_keeps_order(client, db):
    made = [put(db, "bosun", f"P{i}") for i in range(N)]
    oldest, second = made[0], made[1]
    store.set_pinned(db, oldest.id, True, agent=None)
    decisions.ask(
        db,
        agent="bosun",
        artifact_id=second.id,
        question="Which?",
        options=[{"letter": "a", "label": "Yes"}, {"letter": "b", "label": "No"}],
        recommended="a",
    )
    pages = walk(client, "/")
    flat = [i for p in pages for i in p]
    assert flat[0] == oldest.id and flat[1] == second.id
    assert len(flat) == N and len(set(flat)) == N


def test_filters_search_and_view_survive_on_the_link(client, db):
    for i in range(N):
        put(db, "bosun", f"Alpha {i}")
    html = client.get("/?q=alpha&agent=bosun&view=list").text
    href = older_href(html)
    assert href is not None
    q = parse_qs(urlparse(href).query)
    assert q["q"] == ["alpha"] and q["agent"] == ["bosun"] and q["view"] == ["list"]
    assert "older" in q
    second = client.get(href).text
    assert len(ids(second)) == N - 200 and older_href(second) is None


def test_the_archived_tab_pages_too_and_the_live_count_holds(client, db):
    made = [put(db, "bosun", f"P{i}").id for i in range(N)]
    for i in made:
        store.set_archived(db, i, True, agent=None)
    live = put(db, "bosun", "Live one")
    first = client.get("/?filter=archived").text
    assert len(ids(first)) == 200
    href = older_href(first)
    assert href and "filter=archived" in href
    second = client.get(href).text
    assert len(ids(second)) == N - 200 and older_href(second) is None
    assert live.id not in ids(first) + ids(second)


def test_a_folder_pages_and_its_tile_shows_only_on_the_first_page(client, db):
    made = [
        put(db, "bosun", f"D{i}", folder="digests", folder_title="Digests").id
        for i in range(N)
    ]
    with db.connect() as conn:
        fid = conn.execute("select id from folder where slug = 'digests'").fetchone()[0]
    pages = walk(client, f"/f/{fid}")
    assert [len(p) for p in pages] == [200, N - 200]
    assert {i for p in pages for i in p} == set(made)
    # The wall itself shows the folder as one tile and no link: it holds no loose pages.
    top = client.get("/").text
    assert older_href(top) is None and f'href="/f/{fid}"' in top


def test_folder_tiles_are_not_repeated_on_a_later_page(client, db):
    put(db, "bosun", "In folder", folder="f1", folder_title="Shelf")
    for i in range(N):
        put(db, "bosun", f"P{i}")
    first = client.get("/").text
    second = client.get(older_href(first)).text
    assert "Shelf" in first and "Shelf" not in second


def test_a_bad_cursor_is_ignored(client, db):
    put(db, "bosun", "Only")
    assert client.get("/?older=garbage").status_code == 200
    assert client.get("/?older=a|b|c|d|e").status_code == 200
